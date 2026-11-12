"use client";

import React, { useMemo, useState } from "react";
import { toast } from "sonner";
import * as hrApi from "@/lib/api/hr/hr";
import { PracticeTypePicker } from "@/components/hr/practice-type-picker";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { BonusPracticeType, HREmployee } from "@/types/hr";
import {
  REASON_MAX,
  validateManualBonus,
  witaToday,
  type ManualBonusDraft,
  type ManualBonusErrors,
} from "./manual-bonus-form";

export const DEPARTMENT_LABEL: Record<string, string> = {
  setup: "Setup",
  tax: "Tax",
  office: "Office",
};
const DEPARTMENT_ORDER = ["setup", "tax", "office"];

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  employees: readonly HREmployee[];
  types: readonly BonusPracticeType[];
  /** Called after the server accepted the bonus, so the page reloads its ledger. */
  onCreated: () => void;
}

const FIELD: React.CSSProperties = {
  background: "var(--bz-surface)",
  borderColor: "var(--bz-border)",
  color: "var(--bz-text-1)",
};

function Err({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p
      id={id}
      role="alert"
      className="mt-1 text-xs"
      style={{ color: "var(--state-danger)" }}
    >
      {message}
    </p>
  );
}

export function AddBonusDialog({
  open,
  onOpenChange,
  employees,
  types,
  onCreated,
}: Props) {
  const blank = (): ManualBonusDraft => ({
    employeeId: "",
    amount: "",
    reason: "",
    serviceCode: "",
    awardedOn: witaToday(),
  });
  const [draft, setDraft] = useState<ManualBonusDraft>(blank);
  const [errors, setErrors] = useState<ManualBonusErrors>({});
  const [saving, setSaving] = useState(false);

  const people = useMemo(() => {
    const groups = new Map<string, HREmployee[]>();
    for (const e of employees) {
      const dep = e.department || "office";
      groups.set(dep, [...(groups.get(dep) ?? []), e]);
    }
    return [...groups.entries()]
      .sort(
        ([a], [b]) =>
          (DEPARTMENT_ORDER.indexOf(a) + 1 || 99) -
          (DEPARTMENT_ORDER.indexOf(b) + 1 || 99),
      )
      .map(([dep, list]) => ({
        dep,
        list: [...list].sort((a, b) =>
          (a.full_name || a.email).localeCompare(b.full_name || b.email),
        ),
      }));
  }, [employees]);

  const set = (patch: Partial<ManualBonusDraft>) =>
    setDraft((d) => ({ ...d, ...patch }));

  const close = (next: boolean) => {
    if (!next) {
      setDraft(blank());
      setErrors({});
    }
    onOpenChange(next);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const found = validateManualBonus(draft);
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    setSaving(true);
    try {
      await hrApi.createManualBonus({
        employee_id: Number(draft.employeeId),
        amount_idr: Number(draft.amount),
        reason: draft.reason.trim(),
        practice_type_code: draft.serviceCode || null,
        awarded_on: draft.awardedOn,
      });
      toast.success("Bonus added — waiting for approval");
      close(false);
      onCreated();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to add bonus");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent
        className="max-w-md rounded-3xl"
        style={{
          background: "var(--bz-card)",
          borderColor: "var(--bz-border)",
          color: "var(--bz-text-1)",
        }}
      >
        <DialogHeader>
          <DialogTitle>Add a bonus</DialogTitle>
          <DialogDescription style={{ color: "var(--bz-text-2)" }}>
            For work that did not go through a completed practice — tax services
            included. It starts as pending, like any bonus.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-3.5" noValidate>
          <div>
            <label
              htmlFor="bonus-person"
              className="mb-1 block text-xs font-medium"
            >
              Person
            </label>
            <select
              id="bonus-person"
              value={draft.employeeId}
              onChange={(e) => set({ employeeId: e.target.value })}
              aria-invalid={!!errors.employeeId}
              aria-describedby="bonus-person-err"
              className="w-full rounded-xl border px-3 py-2 text-sm"
              style={FIELD}
            >
              <option value="">Choose a person</option>
              {people.map((g) => (
                <optgroup key={g.dep} label={DEPARTMENT_LABEL[g.dep] ?? g.dep}>
                  {g.list.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.full_name || p.email}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <Err id="bonus-person-err" message={errors.employeeId} />
          </div>

          <div>
            <span className="mb-1 block text-xs font-medium">
              Service{" "}
              <span style={{ color: "var(--bz-text-2)" }}>(optional)</span>
            </span>
            <PracticeTypePicker
              ariaLabel="Service"
              types={types}
              value={draft.serviceCode}
              onChange={(serviceCode) => set({ serviceCode })}
              placeholder="No specific service"
              clearable
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label
                htmlFor="bonus-amount"
                className="mb-1 block text-xs font-medium"
              >
                Amount (IDR)
              </label>
              <input
                id="bonus-amount"
                type="number"
                min={1}
                inputMode="numeric"
                value={draft.amount}
                onChange={(e) => set({ amount: e.target.value })}
                aria-invalid={!!errors.amount}
                aria-describedby="bonus-amount-err"
                placeholder="250000"
                className="w-full rounded-xl border px-3 py-2 text-sm"
                style={FIELD}
              />
              <Err id="bonus-amount-err" message={errors.amount} />
            </div>
            <div>
              <label
                htmlFor="bonus-date"
                className="mb-1 block text-xs font-medium"
              >
                Date
              </label>
              <input
                id="bonus-date"
                type="date"
                value={draft.awardedOn}
                onChange={(e) => set({ awardedOn: e.target.value })}
                aria-invalid={!!errors.awardedOn}
                aria-describedby="bonus-date-err"
                className="w-full rounded-xl border px-3 py-2 text-sm"
                style={FIELD}
              />
              <Err id="bonus-date-err" message={errors.awardedOn} />
            </div>
          </div>

          <div>
            <label
              htmlFor="bonus-reason"
              className="mb-1 flex justify-between text-xs font-medium"
            >
              <span>Reason</span>
              <span style={{ color: "var(--bz-text-2)" }}>
                {draft.reason.length}/{REASON_MAX}
              </span>
            </label>
            <textarea
              id="bonus-reason"
              rows={3}
              maxLength={REASON_MAX}
              value={draft.reason}
              onChange={(e) => set({ reason: e.target.value })}
              aria-invalid={!!errors.reason}
              aria-describedby="bonus-reason-err"
              placeholder="What was it for?"
              className="w-full resize-none rounded-xl border px-3 py-2 text-sm"
              style={FIELD}
            />
            <Err id="bonus-reason-err" message={errors.reason} />
          </div>

          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={() => close(false)}
              className="rounded-full px-4 py-2 text-sm"
              style={{ color: "var(--bz-text-2)" }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="rounded-full bg-[var(--bz-accent)] px-5 py-2 text-sm font-semibold text-[var(--bz-on-warm)] disabled:opacity-50"
            >
              {saving ? "Adding…" : "Add bonus"}
            </button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
