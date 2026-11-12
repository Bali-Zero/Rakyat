"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type {
  InvoiceCompanyChoice,
  InvoiceCompanyMatch,
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
const SEARCH_MIN_CHARS = 2;
const SEARCH_DEBOUNCE_MS = 250;

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

/** A typed NPWP without a company name is a draft that cannot be sent. */
export function invoiceCompanyIncomplete(value: InvoiceCompanyValue): boolean {
  return (
    value.companyId === null && !value.name.trim() && Boolean(value.npwp.trim())
  );
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
  onPickCandidate?: (id: number, name: string) => void;
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
  // Live matches for the typed name; kept with the query they answer so a list
  // for an older query is never shown, and the pick made from it (name shown in
  // the selector) for a company the suggestion did not list.
  const [search, setSearch] = useState<{
    ctx: string;
    q: string;
    rows: InvoiceCompanyMatch[];
  } | null>(null);
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  const [picked, setPicked] = useState<InvoiceCompanyChoice | null>(null);
  const touched = useRef(false);
  const autoFilled = useRef(false);
  const prefillId = useRef<number | null>(null);
  const lastClient = useRef<number | undefined>(clientId);
  const lastCode = useRef<string | undefined>(practiceTypeCode);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const valueRef = useRef(value);
  valueRef.current = value;
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;
  // True only while the field still holds exactly what this component prefilled.
  const holdsOwnPrefill = () =>
    autoFilled.current &&
    valueRef.current.companyId !== null &&
    valueRef.current.companyId === prefillId.current &&
    !valueRef.current.name &&
    !valueRef.current.npwp;

  useEffect(() => {
    if (lastClient.current !== clientId) {
      lastClient.current = clientId;
      touched.current = false;
      autoFilled.current = false;
      prefillId.current = null;
      setTyping(false);
      setCompanies([]);
      setLoaded(false);
      setSearch(null);
      setDismissedFor(null);
      setPicked(null);
      onChangeRef.current(EMPTY_INVOICE_COMPANY);
    }
    if (!autofill) {
      // A saved or non-autofill draft is the user's: retire any prefill ownership.
      autoFilled.current = false;
      prefillId.current = null;
    }
    if (lastCode.current !== practiceTypeCode) {
      lastCode.current = practiceTypeCode;
      if (autofill && holdsOwnPrefill() && !touched.current) {
        // The service changed: what was prefilled for the old one is stale until
        // the new suggestion says otherwise (it may be slow or fail).
        autoFilled.current = false;
        onChangeRef.current(EMPTY_INVOICE_COMPANY);
      }
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
        if (!autofill || touched.current || disabledRef.current) return;
        const current = valueRef.current;
        const isEmpty =
          current.companyId === null && !current.name && !current.npwp;
        // A suggestion only ever fills an empty field or replaces its own earlier
        // prefill; whatever the user chose, typed or picked is never overwritten.
        if (!isEmpty && !holdsOwnPrefill()) return;
        if (s.suggested_company_id !== null) {
          autoFilled.current = true;
          prefillId.current = s.suggested_company_id;
          onChangeRef.current({
            companyId: s.suggested_company_id,
            name: "",
            npwp: "",
          });
        } else if (holdsOwnPrefill()) {
          autoFilled.current = false;
          prefillId.current = null;
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

  const typedQuery = value.name.trim();
  const searchCtx = `${clientId ?? ""}|${practiceTypeCode ?? ""}`;
  const searchActive =
    clientId !== undefined &&
    value.companyId === null &&
    typedQuery.length >= SEARCH_MIN_CHARS &&
    !disabled;
  useEffect(() => {
    if (!searchActive || clientId === undefined) {
      setSearch(null);
      return;
    }
    // A list exists only for a request that succeeded for this text and context:
    // drop whatever an earlier generation left before asking again.
    setSearch(null);
    let cancelled = false;
    const timer = setTimeout(() => {
      Promise.resolve()
        .then(() => api.crm.searchInvoiceCompanies(clientId, typedQuery))
        .then((r) => {
          if (!cancelled)
            setSearch({ ctx: searchCtx, q: typedQuery, rows: r.companies });
        })
        .catch((err) => {
          if (cancelled) return;
          logger.warn(
            "Invoice company search unavailable",
            { component: "InvoiceCompanyField", action: "search" },
            toError(err),
          );
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [searchActive, clientId, typedQuery, searchCtx]);

  // Editing the name (also back to an earlier text) reopens a list the user had closed.
  useEffect(() => {
    setDismissedFor(null);
  }, [typedQuery]);

  const options = [...companies];
  if (
    picked &&
    value.companyId === picked.id &&
    !options.some((c) => c.id === picked.id)
  ) {
    options.push(picked);
  }
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

  // A name or NPWP in the value always keeps the typed inputs on screen, so what
  // is sent is what is shown (e.g. typed after a failed suggestion).
  const noCompany = value.companyId === null;
  const hasTyped = noCompany && Boolean(value.name || value.npwp);
  const showTypedInputs =
    noCompany && (typing || hasTyped || (loaded && options.length === 0));
  const selectValue = !noCompany
    ? String(value.companyId)
    : typing || hasTyped
      ? NEW
      : "";

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
            onChange={(e) => {
              touched.current = true;
              onChange({
                companyId: null,
                name: value.name,
                npwp: e.target.value,
              });
            }}
            className={fieldClass}
            placeholder="NPWP (optional)"
          />
        </div>
      )}
      {searchActive &&
        search?.q === typedQuery &&
        search.ctx === searchCtx &&
        dismissedFor !== typedQuery && (
          <div
            className="rounded-lg border border-[var(--border)] p-2 text-sm space-y-1"
            data-testid="invoice-company-search"
          >
            {search.rows.map((m) => (
              <button
                key={m.id}
                type="button"
                disabled={disabled}
                onClick={() => {
                  touched.current = true;
                  setTyping(false);
                  setSearch(null);
                  setPicked({
                    id: m.id,
                    company_name: m.company_name,
                    npwp_company: null,
                    role: null,
                    is_director: false,
                  });
                  onChange({ companyId: m.id, name: "", npwp: "" });
                }}
                className="block w-full text-left px-3 py-1.5 rounded hover:bg-[var(--background-elevated)]"
              >
                {m.company_name}
                {m.linked && (
                  <span className="ml-2 text-xs text-[var(--foreground-muted)]">
                    linked to this client
                  </span>
                )}
              </button>
            ))}
            <button
              type="button"
              disabled={disabled}
              onClick={() => setDismissedFor(typedQuery)}
              className="block w-full text-left px-3 py-1.5 rounded border border-dashed border-[var(--border)] hover:bg-[var(--background-elevated)]"
            >
              Create &laquo;{typedQuery}&raquo;
            </button>
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
                disabled={disabled}
                onClick={() => onPickCandidate?.(c.id, c.company_name)}
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
