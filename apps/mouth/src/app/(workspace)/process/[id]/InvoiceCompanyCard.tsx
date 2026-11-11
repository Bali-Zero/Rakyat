"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { Practice } from "@/lib/api/crm/crm.types";
import {
  EMPTY_INVOICE_COMPANY,
  InvoiceCompanyField,
  ambiguityFromError,
  invoiceCompanyIncomplete,
  invoiceCompanyParams,
  type InvoiceCompanyAmbiguity,
  type InvoiceCompanyValue,
} from "../InvoiceCompanyField";

interface Props {
  practice: Practice;
  /** Only the invoice fields this card owns, so a delayed response cannot restore older ones. */
  onSaved: (
    patch: Pick<Practice, "invoice_company_id" | "invoice_company_name">,
  ) => void;
}

function savedValue(practice: Practice): InvoiceCompanyValue {
  return practice.invoice_company_id
    ? { ...EMPTY_INVOICE_COMPANY, companyId: practice.invoice_company_id }
    : EMPTY_INVOICE_COMPANY;
}

export function InvoiceCompanyCard({ practice, onSaved }: Props) {
  const [value, setValue] = useState<InvoiceCompanyValue>(() =>
    savedValue(practice),
  );
  const [label, setLabel] = useState<string | null>(null);
  const [ambiguity, setAmbiguity] = useState<InvoiceCompanyAmbiguity | null>(
    null,
  );
  const [isSaving, setIsSaving] = useState(false);
  const hasCompany = Boolean(practice.invoice_company_id);
  const baseline = useRef(practice.invoice_company_id ?? null);
  // What a 409 was computed for: a chooser or a late 409 for another client or
  // service is obsolete and must neither show nor be actionable.
  const contextKey = `${practice.client_id}|${practice.practice_type_code ?? ""}`;
  const contextRef = useRef(contextKey);
  contextRef.current = contextKey;
  useEffect(() => {
    setAmbiguity(null);
  }, [contextKey]);

  // Another save may change the company under this card: follow it while the
  // card is clean, never erase a draft.
  useEffect(() => {
    const next = practice.invoice_company_id ?? null;
    if (baseline.current === next) return;
    const wasClean =
      (value.companyId ?? null) === baseline.current &&
      !value.name &&
      !value.npwp;
    baseline.current = next;
    if (wasClean) {
      setValue(savedValue(practice));
      setLabel(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [practice.invoice_company_id]);

  const typed = invoiceCompanyParams(value);
  const incomplete = invoiceCompanyIncomplete(value);
  const dirty = hasCompany
    ? value.companyId !== practice.invoice_company_id
    : Object.keys(typed).length > 0 || incomplete;

  const save = async (override?: InvoiceCompanyValue) => {
    const next = override ?? value;
    if (invoiceCompanyIncomplete(next)) return;
    const params = invoiceCompanyParams(next);
    const startedFor = contextRef.current;
    setIsSaving(true);
    setAmbiguity(null);
    try {
      const updated = await api.crm.updatePractice(
        practice.id,
        Object.keys(params).length > 0 ? params : { invoice_company_id: null },
      );
      baseline.current = updated.invoice_company_id ?? null;
      onSaved({
        invoice_company_id: updated.invoice_company_id ?? null,
        invoice_company_name: updated.invoice_company_name ?? null,
      });
      setValue(savedValue(updated));
      setLabel(null);
      toast.success(
        updated.invoice_company_name
          ? "Invoice company set"
          : "Invoice goes to the individual",
        updated.invoice_company_name
          ? { description: updated.invoice_company_name }
          : undefined,
      );
    } catch (err) {
      const ambiguous = ambiguityFromError(err, next.name.trim());
      if (ambiguous) {
        if (contextRef.current === startedFor) setAmbiguity(ambiguous);
      } else
        toast.error("Failed to save invoice company", {
          description: (err as Error).message,
        });
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div
      className="rounded-xl p-6"
      style={{
        border: "1px solid var(--bz-border)",
        background: "var(--bz-card)",
      }}
      data-testid="invoice-company-card"
    >
      <h2
        className="text-lg font-semibold mb-3"
        style={{ color: "var(--bz-text-1)" }}
      >
        Invoice to company
      </h2>
      <InvoiceCompanyField
        clientId={practice.client_id}
        practiceTypeCode={practice.practice_type_code}
        value={value}
        onChange={(next) => {
          setAmbiguity(null);
          setLabel(null);
          setValue(next);
        }}
        savedName={label ?? practice.invoice_company_name}
        autofill={!hasCompany}
        ambiguity={ambiguity}
        onPickCandidate={(id, name) => {
          // The pick becomes the draft first, so a failed save is retried as the pick.
          const picked = { companyId: id, name: "", npwp: "" };
          setValue(picked);
          setLabel(name);
          void save(picked);
        }}
        disabled={isSaving}
      />
      {incomplete && (
        <p role="alert" className="mt-2 text-xs text-[var(--state-danger)]">
          Enter the company name too, or clear the NPWP.
        </p>
      )}
      {dirty && (
        <div className="mt-3 flex justify-end">
          <Button onClick={() => save()} disabled={isSaving || incomplete}>
            {isSaving && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
            Save
          </Button>
        </div>
      )}
    </div>
  );
}
