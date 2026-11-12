"use client";

import React, { useId, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";
import type { BonusPracticeType } from "@/types/hr";
import { GROUP_TONE, groupPracticeTypes } from "@/lib/hr/practice-groups";

interface Props {
  types: readonly BonusPracticeType[];
  /** Selected practice-type code, "" for none. */
  value: string;
  onChange: (code: string) => void;
  /** Codes that cannot be picked (e.g. a rate already exists). */
  disabledCodes?: ReadonlySet<string>;
  placeholder?: string;
  /** Show a clear button (the service is optional on a manual bonus). */
  clearable?: boolean;
  ariaLabel: string;
}

const tint = (tone: string, pct: number) =>
  `color-mix(in srgb, ${tone} ${pct}%, transparent)`;

/**
 * Searchable, grouped dropdown of practice types: Visa & KITAS, Company, Tax,
 * Other. Shared by the Settings "Add rate" form and the Add bonus form.
 */
export function PracticeTypePicker({
  types,
  value,
  onChange,
  disabledCodes,
  placeholder = "Choose a service",
  clearable = false,
  ariaLabel,
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const groups = useMemo(
    () => groupPracticeTypes(types, query),
    [types, query],
  );
  const selected = types.find((t) => t.code === value);
  const none = groups.every((g) => g.items.length === 0);

  const pick = (code: string) => {
    onChange(code);
    setOpen(false);
    setQuery("");
  };

  return (
    <div
      ref={rootRef}
      className="relative"
      onBlur={(e) => {
        if (!rootRef.current?.contains(e.relatedTarget as Node | null)) {
          setOpen(false);
        }
      }}
    >
      <div className="flex items-center gap-1">
        <button
          type="button"
          role="combobox"
          aria-label={ariaLabel}
          aria-expanded={open}
          aria-controls={listId}
          aria-haspopup="listbox"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center justify-between gap-2 rounded-xl border px-3 py-2 text-left text-sm"
          style={{
            background: "var(--bz-surface)",
            borderColor: "var(--bz-border)",
            color: selected ? "var(--bz-text-1)" : "var(--bz-text-2)",
          }}
        >
          <span className="truncate">{selected?.name ?? placeholder}</span>
          <ChevronDown size={14} className="shrink-0" />
        </button>
        {clearable && value && (
          <button
            type="button"
            aria-label="Clear service"
            onClick={() => pick("")}
            className="rounded-full p-1.5"
            style={{ color: "var(--bz-text-2)" }}
          >
            <X size={14} />
          </button>
        )}
      </div>

      {open && (
        <div
          className="absolute z-30 mt-1.5 w-full overflow-hidden rounded-2xl border shadow-xl"
          style={{
            background: "var(--bz-card)",
            borderColor: "var(--bz-border)",
          }}
        >
          <div
            className="flex items-center gap-2 border-b px-3 py-2"
            style={{ borderColor: "var(--bz-border)" }}
          >
            <Search size={14} style={{ color: "var(--bz-text-2)" }} />
            <input
              autoFocus
              type="text"
              aria-label="Search services"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search services"
              className="w-full bg-transparent text-sm outline-none"
              style={{ color: "var(--bz-text-1)" }}
            />
          </div>
          <div
            id={listId}
            role="listbox"
            aria-label={ariaLabel}
            className="max-h-72 overflow-y-auto p-1.5"
          >
            {groups.map((g) =>
              g.items.length === 0 ? null : (
                <div
                  key={g.id}
                  role="group"
                  aria-label={g.label}
                  className="mb-1"
                >
                  <div
                    className="flex items-center gap-1.5 px-2 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide"
                    style={{ color: GROUP_TONE[g.id] }}
                  >
                    <span
                      className="inline-block h-2 w-2 rounded-full"
                      style={{ background: GROUP_TONE[g.id] }}
                    />
                    {g.label}
                  </div>
                  {g.items.map((t) => {
                    const disabled = disabledCodes?.has(t.code) ?? false;
                    const active = t.code === value;
                    return (
                      <button
                        key={t.code}
                        type="button"
                        role="option"
                        aria-selected={active}
                        disabled={disabled}
                        onClick={() => pick(t.code)}
                        className="flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm disabled:opacity-40"
                        style={{
                          color: "var(--bz-text-1)",
                          background: active
                            ? tint(GROUP_TONE[g.id], 14)
                            : "transparent",
                        }}
                      >
                        <span className="truncate">{t.name}</span>
                        {disabled ? (
                          <span
                            className="text-[10px]"
                            style={{ color: "var(--bz-text-2)" }}
                          >
                            has a rate
                          </span>
                        ) : (
                          active && <Check size={13} />
                        )}
                      </button>
                    );
                  })}
                </div>
              ),
            )}
            {none && (
              <p
                className="px-3 py-4 text-center text-sm"
                style={{ color: "var(--bz-text-2)" }}
              >
                No service matches “{query}”.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
