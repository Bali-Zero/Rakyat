"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Check, Pencil, Plus, Receipt, Sparkles, X } from "lucide-react";
import { toast } from "sonner";
import * as hrApi from "@/lib/api/hr/hr";
import { PracticeTypePicker } from "@/components/hr/practice-type-picker";
import {
  GROUP_TONE,
  PRACTICE_GROUPS,
  groupOfCode,
} from "@/lib/hr/practice-groups";
import type {
  BonusPracticeType,
  BonusRate,
  BonusRatePayload,
  PracticeGroupId,
} from "@/types/hr";
import { Money } from "@balizero/core";

/** Dashboard panel recipe — mirrors the day/dark-aware Kita surfaces. */
const PANEL: React.CSSProperties = {
  background: "var(--bz-card)",
  borderColor: "var(--bz-border)",
};

/** Form controls on the panel surface. */
const INPUT_STYLE: React.CSSProperties = {
  background: "var(--bz-surface)",
  borderColor: "var(--bz-border)",
};

const tint = (tone: string, pct: number) =>
  `color-mix(in srgb, ${tone} ${pct}%, transparent)`;

/** Keep what an upsert would otherwise blank (dates, notes) when one field changes. */
function payloadOf(
  rate: BonusRate,
  over: Partial<BonusRatePayload>,
): BonusRatePayload {
  return {
    practice_type_code: rate.practice_type_code,
    amount_idr: rate.amount_idr,
    effective_from: rate.effective_from ?? undefined,
    effective_to: rate.effective_to ?? undefined,
    is_active: rate.is_active,
    notes: rate.notes ?? undefined,
    ...over,
  };
}

export default function HRSettingsPage() {
  const [rates, setRates] = useState<BonusRate[]>([]);
  const [types, setTypes] = useState<BonusPracticeType[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editAmount, setEditAmount] = useState("");
  const [newCode, setNewCode] = useState("");
  const [newAmount, setNewAmount] = useState("");
  const [onlyGroup, setOnlyGroup] = useState<PracticeGroupId | null>(null);
  const [showNew, setShowNew] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.allSettled([
      hrApi.listBonusRates(),
      hrApi.listBonusPracticeTypes(),
    ]).then(([r, t]) => {
      if (!alive) return;
      if (r.status === "fulfilled") setRates(r.value.rates || []);
      if (t.status === "fulfilled") setTypes(t.value.practice_types || []);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, []);

  const rateCodes = useMemo(
    () => new Set(rates.map((r) => r.practice_type_code)),
    [rates],
  );

  const pickable = useMemo(
    () => (onlyGroup ? types.filter((t) => t.group === onlyGroup) : types),
    [types, onlyGroup],
  );

  const byGroup = useMemo(() => {
    const out: Record<PracticeGroupId, BonusRate[]> = {
      visa: [],
      company: [],
      tax: [],
      other: [],
    };
    for (const r of rates) {
      out[groupOfCode(r.practice_type_code, types)].push(r);
    }
    for (const list of Object.values(out)) {
      list.sort((a, b) =>
        (a.practice_type_name || a.practice_type_code).localeCompare(
          b.practice_type_name || b.practice_type_code,
        ),
      );
    }
    return out;
  }, [rates, types]);

  const openAdd = (group: PracticeGroupId | null) => {
    setOnlyGroup(group);
    setNewCode("");
    setShowNew(true);
  };

  const replaceRate = (saved: BonusRate) =>
    setRates((prev) =>
      prev.map((r) =>
        r.practice_type_code === saved.practice_type_code
          ? { ...r, ...saved, practice_type_name: r.practice_type_name }
          : r,
      ),
    );

  const save = async (rate: BonusRate, over: Partial<BonusRatePayload>) => {
    try {
      const result = await hrApi.upsertBonusRate(payloadOf(rate, over));
      replaceRate(result.rate ?? { ...rate, ...over });
      return true;
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to save rate");
      return false;
    }
  };

  const handleSaveAmount = async (rate: BonusRate) => {
    const amount = Number(editAmount);
    if (!Number.isFinite(amount) || amount < 0) return;
    if (await save(rate, { amount_idr: amount })) {
      setEditingId(null);
      toast.success("Rate saved");
    }
  };

  const handleToggle = async (rate: BonusRate) => {
    if (await save(rate, { is_active: !rate.is_active })) {
      toast.success(rate.is_active ? "Rate switched off" : "Rate switched on");
    }
  };

  const handleCreate = async () => {
    const amount = Number(newAmount);
    if (!newCode || !newAmount || !Number.isFinite(amount) || amount < 0) {
      return;
    }
    try {
      const result = await hrApi.upsertBonusRate({
        practice_type_code: newCode,
        amount_idr: amount,
      });
      const name = types.find((t) => t.code === newCode)?.name ?? null;
      setRates((prev) => [
        ...prev.filter((r) => r.practice_type_code !== newCode),
        { ...result.rate, practice_type_name: name },
      ]);
      setNewCode("");
      setNewAmount("");
      setShowNew(false);
      setOnlyGroup(null);
      toast.success("Rate created");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to create rate");
    }
  };

  if (loading) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">
          HR Settings
        </h1>
        <div className="animate-pulse space-y-3">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-12 border rounded-lg" style={PANEL} />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-[var(--bz-text-1)]">
            Bonus Rate Configuration
          </h1>
          <p className="mt-1 text-sm text-[var(--bz-text-2)]">
            New visas earn their bonus when staff upload the Electronic Visa. KITAS
            renewals earn it when staff upload the renewed IMK. Other services
            earn theirs at final delivery. Review missing rates on the Bonuses page.
          </p>
        </div>
        <button
          type="button"
          onClick={() => (showNew ? setShowNew(false) : openAdd(null))}
          className="flex items-center gap-2 rounded-full px-4 py-2 text-sm font-semibold"
          style={{
            background: tint("var(--bz-accent)", 14),
            color: "var(--bz-accent)",
          }}
        >
          <Plus size={16} />
          Add Rate
        </button>
      </div>

      {showNew && (
        <div
          className="rounded-2xl border p-4"
          style={{ ...PANEL, borderColor: "var(--bz-border-accent)" }}
        >
          <div className="grid gap-3 sm:grid-cols-[1fr_12rem_auto] sm:items-end">
            <div>
              <label className="mb-1 block text-xs text-[var(--bz-text-2)]">
                Service
              </label>
              <PracticeTypePicker
                ariaLabel="Service for the new rate"
                types={pickable}
                value={newCode}
                onChange={setNewCode}
                disabledCodes={rateCodes}
                placeholder={
                  onlyGroup === "tax"
                    ? "Choose a tax service"
                    : "Choose a service"
                }
              />
              {onlyGroup && (
                <button
                  type="button"
                  onClick={() => setOnlyGroup(null)}
                  className="mt-1 text-xs text-[var(--bz-accent)] hover:underline"
                >
                  Showing {onlyGroup} services only — show all
                </button>
              )}
            </div>
            <div>
              <label
                htmlFor="new-rate-amount"
                className="mb-1 block text-xs text-[var(--bz-text-2)]"
              >
                Amount (IDR)
              </label>
              <input
                id="new-rate-amount"
                type="number"
                min={0}
                value={newAmount}
                onChange={(e) => setNewAmount(e.target.value)}
                placeholder="500000"
                className="w-full rounded-xl border px-3 py-2 text-sm text-[var(--bz-text-1)]"
                style={INPUT_STYLE}
              />
            </div>
            <button
              type="button"
              onClick={handleCreate}
              disabled={!newCode || !newAmount}
              className="rounded-full bg-[var(--bz-accent)] px-5 py-2 text-sm font-semibold text-[var(--bz-on-warm)] disabled:opacity-40"
            >
              Create
            </button>
          </div>
        </div>
      )}

      <div className="space-y-7">
        {PRACTICE_GROUPS.map((g) => {
          const list = byGroup[g.id];
          if (list.length === 0 && g.id !== "tax") return null;
          const tone = GROUP_TONE[g.id];
          return (
            <section key={g.id} aria-label={`${g.label} rates`}>
              <div className="mb-2.5 flex items-center gap-2">
                <span
                  className="inline-block h-2.5 w-2.5 rounded-full"
                  style={{ background: tone }}
                />
                <h2
                  className="text-sm font-semibold uppercase tracking-wide"
                  style={{ color: tone }}
                >
                  {g.id === "tax" ? "Tax services" : g.label}
                </h2>
                <span
                  className="rounded-full px-2 py-0.5 text-[11px] font-semibold"
                  style={{ background: tint(tone, 14), color: tone }}
                >
                  {list.length}
                </span>
                {g.id === "tax" && list.length > 0 && (
                  <button
                    type="button"
                    onClick={() => openAdd("tax")}
                    className="ml-auto flex items-center gap-1 text-xs font-semibold"
                    style={{ color: tone }}
                  >
                    <Plus size={13} /> Add a tax rate
                  </button>
                )}
              </div>

              {list.length === 0 ? (
                <div
                  className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-dashed p-5"
                  style={{
                    borderColor: tint(tone, 45),
                    background: tint(tone, 6),
                  }}
                >
                  <div className="flex items-center gap-3">
                    <span
                      className="flex h-10 w-10 items-center justify-center rounded-full"
                      style={{ background: tint(tone, 16), color: tone }}
                    >
                      <Receipt size={18} />
                    </span>
                    <p className="text-sm text-[var(--bz-text-1)]">
                      No tax rate yet.
                      <span className="block text-xs text-[var(--bz-text-2)]">
                        Set a rate for future tax services. Review earned services
                        awaiting a rate on the Bonuses page.
                      </span>
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => openAdd("tax")}
                    className="flex items-center gap-1.5 rounded-full px-4 py-2 text-sm font-semibold"
                    style={{ background: tone, color: "var(--bz-on-warm)" }}
                  >
                    <Sparkles size={14} />
                    Add a tax rate
                  </button>
                </div>
              ) : (
                <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
                  {list.map((rate) => (
                    <div
                      key={rate.id}
                      className="flex flex-col gap-3 rounded-2xl border p-3.5"
                      style={{
                        ...PANEL,
                        opacity: rate.is_active ? 1 : 0.6,
                        borderLeft: `3px solid ${tone}`,
                      }}
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-[var(--bz-text-1)]">
                          {rate.practice_type_name ||
                            rate.practice_type_code?.replace(/_/g, " ")}
                        </div>
                        <div className="truncate text-[11px] text-[var(--bz-text-3)]">
                          {rate.practice_type_code}
                        </div>
                      </div>
                      <div className="flex items-center justify-between gap-2">
                        {editingId === rate.id ? (
                          <div className="flex items-center gap-1.5">
                            <input
                              type="number"
                              min={0}
                              aria-label="New amount"
                              value={editAmount}
                              onChange={(e) => setEditAmount(e.target.value)}
                              className="w-28 rounded-lg border px-2 py-1 text-sm text-[var(--bz-text-1)]"
                              style={INPUT_STYLE}
                            />
                            <button
                              type="button"
                              aria-label="Save amount"
                              onClick={() => handleSaveAmount(rate)}
                              className="rounded-full p-1.5"
                              style={{
                                color: "var(--state-success)",
                                background: tint("var(--state-success)", 14),
                              }}
                            >
                              <Check size={14} />
                            </button>
                            <button
                              type="button"
                              aria-label="Cancel edit"
                              onClick={() => setEditingId(null)}
                              className="rounded-full p-1.5 text-[var(--bz-text-2)]"
                            >
                              <X size={14} />
                            </button>
                          </div>
                        ) : (
                          <button
                            type="button"
                            aria-label={`Edit amount for ${rate.practice_type_name || rate.practice_type_code}`}
                            onClick={() => {
                              setEditingId(rate.id);
                              setEditAmount(String(rate.amount_idr));
                            }}
                            className="group flex items-center gap-1.5 rounded-full px-3 py-1 text-sm font-semibold"
                            style={{ background: tint(tone, 14), color: tone }}
                          >
                            <Money value={rate.amount_idr} />
                            <Pencil size={11} className="opacity-60" />
                          </button>
                        )}
                        <button
                          type="button"
                          role="switch"
                          aria-checked={rate.is_active}
                          aria-label={`${rate.practice_type_name || rate.practice_type_code} active`}
                          onClick={() => handleToggle(rate)}
                          className="relative h-5 w-9 shrink-0 rounded-full transition-colors"
                          style={{
                            background: rate.is_active
                              ? "var(--state-success)"
                              : "var(--bz-border)",
                          }}
                        >
                          <span
                            className="absolute top-0.5 h-4 w-4 rounded-full bg-[var(--bz-card)] transition-all"
                            style={{
                              left: rate.is_active ? "1.1rem" : "0.15rem",
                            }}
                          />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
