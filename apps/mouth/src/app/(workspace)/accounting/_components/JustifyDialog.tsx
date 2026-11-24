"use client";

import { useMemo, useRef, useState } from "react";
import { Loader2, CheckCircle2, X, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Money } from "@balizero/core";
import type {
  JustifyOutgoingRequest,
  OutgoingCategory,
  OutgoingItem,
} from "@/lib/api/workspace/accounting.api";
import { CasePicker, type PickedCase } from "./CasePicker";

export const CATEGORY_LABELS: Record<OutgoingCategory, string> = {
  pnbp_case: "PNBP for a case",
  vendor_case: "Notary / agent / freelancer for a case",
  staff_reimbursement: "Staff reimbursement",
  payroll: "Payroll",
  cash_withdrawal: "Cash withdrawal",
  bank_charges: "Bank charges",
  tax: "Tax",
  own_transfer: "Transfer between own accounts",
  other: "Other",
};

const CATEGORIES = Object.keys(CATEGORY_LABELS) as OutgoingCategory[];
const LINE_CATEGORIES = CATEGORIES.filter((c) => c !== "cash_withdrawal");

/** The category the row's automatic reading points to, where it is obvious. */
const FROM_KIND: Record<string, OutgoingCategory> = {
  government_fee: "pnbp_case",
  bank_fee: "bank_charges",
  tax: "tax",
  payroll: "payroll",
  cash_withdrawal: "cash_withdrawal",
};

interface DraftLine {
  key: number;
  amount: string;
  category: OutgoingCategory;
  note: string;
  case: PickedCase | null;
  /** Something is typed in the line's case box and no case is picked yet. */
  caseTyping: boolean;
}

/**
 * A typed amount in whole rupiah, or null. Plain digits, or digits grouped by three with
 * "." or "," (1.000.000); a sign, a decimal part or any other text is not an amount, so it
 * is never turned into a different number.
 */
const parseRupiah = (raw: string): number | null => {
  const t = raw.trim();
  if (/^\d+$/.test(t)) return Number(t);
  if (/^\d{1,3}([.,]\d{3})+$/.test(t)) return Number(t.replace(/[.,]/g, ""));
  return null;
};
const inputCls =
  "w-full rounded-md border border-[var(--bz-border)] bg-transparent px-2 py-1.5 text-sm";

export function JustifyDialog({
  item,
  onClose,
  onSave,
}: {
  item: OutgoingItem;
  onClose: () => void;
  /** Resolves when saved; rejects with the server's message, which the dialog shows. */
  onSave: (body: JustifyOutgoingRequest) => Promise<void>;
}) {
  const [category, setCategory] = useState<OutgoingCategory>(
    FROM_KIND[item.kind] ?? "other",
  );
  const [picked, setPicked] = useState<PickedCase | null>(null);
  const [caseTyping, setCaseTyping] = useState(false);
  const [recipient, setRecipient] = useState("");
  const [note, setNote] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([]);
  const nextKey = useRef(1);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [failure, setFailure] = useState<string | null>(null);

  const cash = category === "cash_withdrawal";
  const sum = lines.reduce((t, l) => t + (parseRupiah(l.amount) ?? 0), 0);
  const remaining = item.amount_idr - sum;

  const problem = useMemo(() => {
    if (caseTyping) return "Pick the case from the list, or clear the box.";
    if (category === "other" && !note.trim()) return "Other needs a note.";
    if (cash) {
      if (!recipient.trim()) return "Who received the cash?";
      if (!note.trim()) return "A cash withdrawal needs a note.";
      if (lines.length === 0) return "Add at least one line.";
      if (
        lines.some((l) => !((parseRupiah(l.amount) ?? 0) > 0) || !l.note.trim())
      )
        return "Every line needs an amount and a note.";
      if (lines.some((l) => l.caseTyping))
        return "Pick each line's case from the list, or clear the box.";
      if (remaining !== 0) return "The lines must add up to the withdrawal.";
    }
    return null;
  }, [caseTyping, category, note, cash, recipient, lines, remaining]);

  const setLine = (key: number, patch: Partial<DraftLine>) =>
    setLines((ls) => ls.map((l) => (l.key === key ? { ...l, ...patch } : l)));

  const save = async () => {
    if (inFlight.current || problem) return;
    inFlight.current = true;
    setBusy(true);
    setFailure(null);
    const body: JustifyOutgoingRequest = { category };
    if (picked) body.practice_id = picked.id;
    if (cash) body.recipient = recipient.trim();
    if (note.trim()) body.note = note.trim();
    if (cash) {
      body.lines = lines.map((l) => ({
        amount_idr: parseRupiah(l.amount) as number,
        category: l.category,
        note: l.note.trim(),
        ...(l.case ? { practice_id: l.case.id } : {}),
      }));
    }
    try {
      await onSave(body);
    } catch (err) {
      setFailure((err instanceof Error && err.message) || "Please try again");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-label="Justify this outgoing"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    >
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-[var(--bz-border)] bg-[var(--bz-card)] p-6 shadow-xl">
        <div className="flex items-start justify-between">
          <h3 className="text-lg font-semibold">Justify this outgoing</h3>
          <button
            onClick={onClose}
            disabled={busy}
            className="text-[var(--bz-text-2)] hover:text-[var(--bz-text-1)] disabled:opacity-50"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <dl className="mt-4 space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-[var(--bz-text-2)]">Amount</dt>
            <dd className="font-medium tabular-nums">
              <Money value={item.amount_idr} />
            </dd>
          </div>
          <div className="flex justify-between gap-3">
            <dt className="whitespace-nowrap text-[var(--bz-text-2)]">Date</dt>
            <dd className="font-medium tabular-nums">{item.txn_date}</dd>
          </div>
          <div className="flex justify-between gap-3">
            <dt className="whitespace-nowrap text-[var(--bz-text-2)]">
              Bank text
            </dt>
            <dd className="min-w-0 break-words text-right font-medium [overflow-wrap:anywhere]">
              {item.reading.detail}
            </dd>
          </div>
        </dl>

        <div className="mt-4 space-y-3 text-sm">
          <label className="block">
            <span className="text-[var(--bz-text-2)]">Category</span>
            <select
              aria-label="Category"
              className={inputCls}
              value={category}
              onChange={(e) => setCategory(e.target.value as OutgoingCategory)}
            >
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {CATEGORY_LABELS[c]}
                </option>
              ))}
            </select>
          </label>
          <div>
            <span className="text-[var(--bz-text-2)]">Case (optional)</span>
            <CasePicker
              label="Case"
              value={picked}
              onChange={setPicked}
              onTypingChange={setCaseTyping}
            />
          </div>
          {cash && (
            <label className="block">
              <span className="text-[var(--bz-text-2)]">Received by</span>
              <input
                aria-label="Recipient"
                className={inputCls}
                value={recipient}
                onChange={(e) => setRecipient(e.target.value)}
              />
            </label>
          )}
          <label className="block">
            <span className="text-[var(--bz-text-2)]">
              Note{category === "other" || cash ? " (required)" : " (optional)"}
            </span>
            <textarea
              aria-label="Note"
              rows={2}
              className={inputCls}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
        </div>

        {cash && (
          <div className="mt-4 space-y-2" aria-label="Breakdown">
            <p className="text-sm font-semibold">What the cash was for</p>
            {lines.map((l, idx) => (
              <div
                key={l.key}
                data-testid={`justify-line-${idx}`}
                className="space-y-2 rounded-md border border-[var(--bz-border)] p-2"
              >
                <div className="grid grid-cols-2 gap-2">
                  <input
                    aria-label={`Line ${idx + 1} amount`}
                    inputMode="numeric"
                    placeholder="Amount (Rp)"
                    className={inputCls}
                    value={l.amount}
                    onChange={(e) => setLine(l.key, { amount: e.target.value })}
                  />
                  <select
                    aria-label={`Line ${idx + 1} category`}
                    className={inputCls}
                    value={l.category}
                    onChange={(e) =>
                      setLine(l.key, {
                        category: e.target.value as OutgoingCategory,
                      })
                    }
                  >
                    {LINE_CATEGORIES.map((c) => (
                      <option key={c} value={c}>
                        {CATEGORY_LABELS[c]}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="grid grid-cols-[1fr_auto] gap-2">
                  <input
                    aria-label={`Line ${idx + 1} note`}
                    placeholder="Note"
                    className={inputCls}
                    value={l.note}
                    onChange={(e) => setLine(l.key, { note: e.target.value })}
                  />
                  <button
                    type="button"
                    aria-label={`Remove line ${idx + 1}`}
                    className="text-[var(--bz-text-2)] hover:text-[var(--bz-text-1)]"
                    onClick={() =>
                      setLines((ls) => ls.filter((x) => x.key !== l.key))
                    }
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
                <CasePicker
                  label={`Line ${idx + 1} case`}
                  value={l.case}
                  onChange={(c) => setLine(l.key, { case: c })}
                  onTypingChange={(t) => setLine(l.key, { caseTyping: t })}
                />
              </div>
            ))}
            <div className="flex items-center justify-between">
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setLines((ls) => [
                    ...ls,
                    {
                      key: nextKey.current++,
                      amount: "",
                      category: "other",
                      note: "",
                      case: null,
                      caseTyping: false,
                    },
                  ])
                }
              >
                <Plus className="mr-1 h-4 w-4" /> Add a line
              </Button>
              <p
                data-testid="justify-remaining"
                className={`text-sm tabular-nums ${
                  remaining === 0
                    ? "text-[var(--state-success)]"
                    : "text-[var(--state-warning)]"
                }`}
              >
                Remaining: <Money value={remaining} />
              </p>
            </div>
          </div>
        )}

        {problem && (
          <p className="mt-4 text-xs text-[var(--state-warning)]">{problem}</p>
        )}
        {failure && (
          <p role="alert" className="mt-2 text-xs text-[var(--state-danger)]">
            {failure}
          </p>
        )}
        <p className="mt-4 text-xs text-[var(--bz-text-2)]">
          This records why the money left the account. It does not touch the
          cash-out. You can undo it today.
        </p>

        <div className="mt-6 flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={save} disabled={busy || problem !== null}>
            {busy ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <CheckCircle2 className="mr-2 h-4 w-4" />
            )}
            Save
          </Button>
        </div>
      </div>
    </div>
  );
}
