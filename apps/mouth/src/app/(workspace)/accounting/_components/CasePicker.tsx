"use client";

import { useEffect, useState } from "react";
import { Loader2, Search } from "lucide-react";
import { api } from "@/lib/api";
import type { Practice } from "@/lib/api/crm/crm.types";
import type { OutgoingCase } from "@/lib/api/workspace/accounting.api";

/** A case picked in the list: its id, and the words the accountant reads for it. */
export interface PickedCase extends OutgoingCase {
  id: number;
}

/** «Client — Service»: how a case reads, in the dialog and on the Justified tab. */
export const caseText = (c: OutgoingCase): string =>
  [c.client || "Client without a name", c.service].filter(Boolean).join(" — ");

const MIN_CHARS = 2;
const SHOWN = 8;
const MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ");

/** «Nov 2026» from an ISO date, or "" when there is none. */
const opened = (iso?: string | null): string => {
  const m = /^(\d{4})-(\d{2})/.exec(iso ?? "");
  return m ? `${MONTHS[Number(m[2]) - 1]} ${m[1]}` : "";
};

const inputCls =
  "w-full rounded-md border border-[var(--bz-border)] bg-transparent py-1.5 pl-7 pr-7 text-sm";

/**
 * Find a case by typing the client's name (or the service, e-mail, phone or case number: the
 * CRM's own search), then click it. While something is typed and nothing is picked the parent
 * hears `typing`, so it never saves a justification that silently lost its case.
 */
export function CasePicker({
  label,
  value,
  onChange,
  onTypingChange,
}: {
  label: string;
  value: PickedCase | null;
  onChange: (picked: PickedCase | null) => void;
  onTypingChange: (typing: boolean) => void;
}) {
  const [text, setText] = useState("");
  const [found, setFound] = useState<Practice[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    // A list exists only for the text now in the box: an older answer arriving late is dropped.
    setFound(null);
    setFailed(false);
    const q = text.trim();
    if (q.length < MIN_CHARS) {
      setSearching(false);
      return;
    }
    setSearching(true);
    let cancelled = false;
    const t = setTimeout(async () => {
      try {
        const rows = await api.crm.getPractices({ search: q, limit: SHOWN });
        if (!cancelled) setFound(rows);
      } catch {
        if (!cancelled) setFailed(true);
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [text]);

  const type = (v: string) => {
    setText(v);
    onTypingChange(v.trim() !== "");
  };

  const pick = (p: Practice) => {
    onChange({
      id: p.id,
      client: p.client_name ?? null,
      service: p.practice_type_name ?? null,
    });
    setText("");
    onTypingChange(false);
  };

  if (value) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-md border border-[var(--bz-border)] px-2 py-1.5 text-sm">
        <span className="min-w-0 break-words font-medium">
          {caseText(value)}
        </span>
        <button
          type="button"
          aria-label={`Change ${label}`}
          className="shrink-0 text-xs font-medium underline"
          onClick={() => onChange(null)}
        >
          Change
        </button>
      </div>
    );
  }

  return (
    <div>
      <div className="relative">
        <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--bz-text-2)]" />
        <input
          aria-label={label}
          placeholder="Type the client's name"
          className={inputCls}
          value={text}
          onChange={(e) => type(e.target.value)}
        />
        {searching && (
          <Loader2 className="absolute right-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-[var(--bz-text-2)]" />
        )}
      </div>
      {failed && (
        <p className="mt-1 text-xs text-[var(--state-danger)]">
          Could not search the cases. Try again.
        </p>
      )}
      {found && found.length === 0 && (
        <p className="mt-1 text-xs text-[var(--bz-text-2)]">
          No case found. Check the name, or leave the case empty.
        </p>
      )}
      {found && found.length > 0 && (
        <ul
          aria-label={`${label}: cases found`}
          className="mt-1 divide-y divide-[var(--bz-border)] rounded-md border border-[var(--bz-border)]"
        >
          {found.map((p) => {
            const when = opened(p.start_date || p.created_at);
            return (
              <li key={p.id}>
                <button
                  type="button"
                  className="w-full px-2 py-1.5 text-left hover:bg-[var(--bz-surface-2)]"
                  onClick={() => pick(p)}
                >
                  <span className="block text-sm font-medium">
                    {p.client_name || "Client without a name"}
                  </span>
                  <span className="block text-xs text-[var(--bz-text-2)]">
                    {[
                      p.practice_type_name,
                      p.family_member_name && `for ${p.family_member_name}`,
                      when && `opened ${when}`,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
