"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { Practice } from "@/lib/api/crm/crm.types";
import {
  EMPTY_INVOICE_COMPANY,
  InvoiceCompanyField,
  ambiguityFromError,
  invoiceCompanyParams,
  type InvoiceCompanyAmbiguity,
  type InvoiceCompanyValue,
} from "../InvoiceCompanyField";

interface Props {
  practice: Practice;
  onSaved: (practice: Practice) => void;
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
  const [ambiguity, setAmbiguity] = useState<InvoiceCompanyAmbiguity | null>(
    null,
  );
  const [isSaving, setIsSaving] = useState(false);
  const hasCompany = Boolean(practice.invoice_company_id);

  const typed = invoiceCompanyParams(value);
  const dirty = hasCompany
    ? value.companyId !== practice.invoice_company_id
    : Object.keys(typed).length > 0;

  const save = async (override?: InvoiceCompanyValue) => {
    const next = override ?? value;
    const params = invoiceCompanyParams(next);
    setIsSaving(true);
    setAmbiguity(null);
    try {
      const updated = await api.crm.updatePractice(
        practice.id,
        Object.keys(params).length > 0 ? params : { invoice_company_id: null },
      );
      onSaved(updated);
      setValue(savedValue(updated));
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
      if (ambiguous) setAmbiguity(ambiguous);
      else
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
        onChange={setValue}
        savedName={practice.invoice_company_name}
        autofill={!hasCompany}
        ambiguity={ambiguity}
        onPickCandidate={(id) => save({ companyId: id, name: "", npwp: "" })}
        disabled={isSaving}
      />
      {dirty && (
        <div className="mt-3 flex justify-end">
          <Button onClick={() => save()} disabled={isSaving}>
            {isSaving && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
            Save
          </Button>
        </div>
      )}
    </div>
  );
}
