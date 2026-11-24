"use client";
import { useEffect, useRef, useState } from "react";
import {
  accountingApi,
  type CashInvoice,
} from "@/lib/api/workspace/accounting.api";
import { CompanyStrip, useCompanies } from "./CompanyStrip";
import { useToast } from "@/components/ui/toast";
import { formatIDR } from "@balizero/core/utils";

const witaToday = () =>
  new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Makassar" }).format(
    new Date(),
  );

export function CashReceiptForm({ onDone }: { onDone: () => void }) {
  const companies = useCompanies();
  const { success, error } = useToast();
  const [company, setCompany] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [items, setItems] = useState<CashInvoice[]>([]);
  const [invoice, setInvoice] = useState<CashInvoice | null>(null);
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(witaToday);
  const [reference, setReference] = useState("");
  const requestId = useRef(crypto.randomUUID());
  const search = useRef(0);
  const pending = useRef(false);
  const [saving, setSaving] = useState(false);
  const [searchStatus, setSearchStatus] = useState("");
  const canRecord =
    !!invoice &&
    /^\d+$/.test(amount) &&
    Number.isSafeInteger(Number(amount)) &&
    Number(amount) > 0 &&
    Number(amount) <= invoice.outstanding_idr &&
    !!date &&
    date <= witaToday();
  const newRequest = () => {
    requestId.current = crypto.randomUUID();
  };
  useEffect(() => {
    const token = ++search.current;
    setInvoice(null);
    setItems([]);
    setSearchStatus("");
    if (!company || !q.trim()) return;
    setSearchStatus("Searching invoices…");
    void accountingApi
      .getCashInvoices(company, q)
      .then((r) => {
        if (search.current !== token) return;
        setItems(r.items);
        setSearchStatus(
          r.items.length ? "" : "No outstanding invoices match this search.",
        );
      })
      .catch(() => {
        if (search.current === token)
          setSearchStatus("Invoice search failed. Try the search again.");
      });
    return () => {
      search.current++;
    };
  }, [company, q]);
  const submit = async () => {
    if (!company || !invoice || !canRecord || pending.current) return;
    pending.current = true;
    setSaving(true);
    try {
      const r = await accountingApi.createCashReceipt({
        company_key: company,
        invoice_id: invoice.invoice_id,
        amount_idr: Number(amount),
        movement_date: date,
        ...(reference.trim() ? { reference: reference.trim() } : {}),
        request_id: requestId.current,
      });
      success(
        `Cash receipt #${r.cashout_id} recorded`,
        `Outstanding balance ${formatIDR(r.outstanding_idr)}`,
      );
      requestId.current = crypto.randomUUID();
      onDone();
    } catch (cause) {
      error(
        "Check cash receipt",
        cause instanceof Error
          ? cause.message + ". Retry the same receipt if needed."
          : "Confirmation unavailable. Retry the same receipt.",
      );
    } finally {
      pending.current = false;
      setSaving(false);
    }
  };
  return (
    <section className="space-y-4">
      <h1 className="text-2xl font-semibold">Record cash received</h1>
      <CompanyStrip
        companies={{
          ...companies,
          rows: companies.rows.filter(
            (row) => row.key !== "other" && !row.key.startsWith("account:"),
          ),
        }}
        selected={company}
        allowAll={false}
        locked={saving}
        onSelect={(key) => {
          newRequest();
          setCompany(key);
        }}
      />
      {company && (
        <fieldset className="space-y-4" disabled={saving}>
          <label className="block text-sm">
            Find invoice
            <input
              className="mt-1 w-full rounded border p-2"
              value={q}
              maxLength={120}
              onChange={(e) => {
                newRequest();
                setQ(e.target.value);
              }}
            />
          </label>
          {searchStatus && <p role="status">{searchStatus}</p>}
          {items.map((i) => (
            <button
              className="block w-full rounded border p-2 text-left"
              key={i.invoice_id}
              onClick={() => {
                newRequest();
                setInvoice(i);
                setAmount(String(Number(i.outstanding_idr)));
              }}
            >
              {i.invoice_number} · {i.client_name} · {i.service_name} ·{" "}
              {formatIDR(i.outstanding_idr)}
            </button>
          ))}
          {invoice && (
            <>
              <p>
                {invoice.invoice_number} · Outstanding{" "}
                {formatIDR(invoice.outstanding_idr)}
              </p>
              <label className="block text-sm">
                Amount IDR
                <input
                  className="mt-1 w-full rounded border p-2"
                  inputMode="numeric"
                  value={amount}
                  onChange={(e) => {
                    newRequest();
                    setAmount(e.target.value);
                  }}
                />
              </label>
              <label className="block text-sm">
                Effective date
                <input
                  className="mt-1 w-full rounded border p-2"
                  type="date"
                  max={witaToday()}
                  value={date}
                  onChange={(e) => {
                    newRequest();
                    setDate(e.target.value);
                  }}
                />
              </label>
              <label className="block text-sm">
                Reference (optional)
                <input
                  className="mt-1 w-full rounded border p-2"
                  value={reference}
                  maxLength={120}
                  onChange={(e) => {
                    newRequest();
                    setReference(e.target.value);
                  }}
                />
              </label>
              <button
                className="rounded bg-[var(--bz-accent)] px-4 py-2 text-white disabled:opacity-50"
                disabled={saving || !canRecord}
                onClick={() => void submit()}
              >
                {saving ? "Recording…" : "Record cash receipt"}
              </button>
            </>
          )}
        </fieldset>
      )}
    </section>
  );
}
