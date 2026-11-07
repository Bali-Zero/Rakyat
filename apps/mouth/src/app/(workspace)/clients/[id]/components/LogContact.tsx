"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import { api } from "@/lib/api";
import { MessageSquarePlus } from "lucide-react";
import { FOCUS } from "@/components/workspace/r19/tokens";

type Preset = {
  key: "call" | "email" | "meeting" | "note";
  label: string;
  channel: "phone" | "gmail" | "in_person" | null;
};

/** Backend vocabulary (crm_interactions.py): interaction_type + channel. */
export const CONTACT_PRESETS: Preset[] = [
  { key: "call", label: "Call", channel: "phone" },
  { key: "email", label: "Email", channel: "gmail" },
  { key: "meeting", label: "Meeting", channel: "in_person" },
  { key: "note", label: "Note", channel: null },
];

/**
 * "Log contact": one small popover on the Last-contact fact. Pick a kind,
 * optionally add one line, submit. The page refreshes through `onLogged`, so
 * Last contact and its "silent N days" tone recompute from the new record.
 */
export function LogContact({
  clientId,
  teamMember,
  onLogged,
}: {
  clientId: number;
  /** Who is logging; null while the viewer is unknown (the control is disabled). */
  teamMember: string | null;
  onLogged: () => Promise<void> | void;
}) {
  const [open, setOpen] = useState(false);
  const [preset, setPreset] = useState<Preset | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const firstRadioRef = useRef<HTMLInputElement>(null);
  const uid = useId();

  const close = () => {
    setOpen(false);
    setError(null);
    triggerRef.current?.focus();
  };

  useEffect(() => {
    if (open) firstRadioRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!panelRef.current?.contains(t) && !triggerRef.current?.contains(t))
        setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const submit = async () => {
    if (!preset || !teamMember || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.crm.createInteraction({
        client_id: clientId,
        interaction_type: preset.key,
        channel: preset.channel,
        summary: note.trim() || preset.label,
        team_member: teamMember,
      });
      setPreset(null);
      setNote("");
      setOpen(false);
      triggerRef.current?.focus();
      await onLogged();
    } catch (err) {
      const why = err instanceof Error && err.message ? `: ${err.message}` : "";
      setError(`Could not log the contact${why}. Nothing was saved.`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-2 min-w-0">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={!teamMember}
        aria-expanded={open}
        aria-haspopup="dialog"
        data-testid="log-contact-trigger"
        className={`inline-flex min-h-8 items-center gap-1.5 rounded border border-[var(--line-control)] px-2.5 text-[12px] font-medium text-[var(--tx-pure)] hover:bg-[var(--bz-card)] disabled:opacity-50 ${FOCUS}`}
      >
        <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden="true" />
        Log contact
      </button>
      {open && (
        <div
          ref={panelRef}
          role="dialog"
          aria-label="Log contact"
          data-testid="log-contact-panel"
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              close();
            }
          }}
          className="mt-2 box-border w-full min-w-0 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-base)] p-3"
        >
          <fieldset className="m-0 border-0 p-0">
            <legend className="mb-1.5 text-[12px] text-[var(--tx-secondary)]">
              What happened
            </legend>
            <div className="grid grid-cols-2 gap-1.5">
              {CONTACT_PRESETS.map((p, i) => (
                <label
                  key={p.key}
                  className={`inline-flex min-h-8 min-w-0 cursor-pointer items-center justify-center rounded border px-2.5 text-[12px] font-medium ${
                    preset?.key === p.key
                      ? "border-[var(--tx-pure)] bg-[var(--tx-pure)] text-[var(--bz-base)]"
                      : "border-[var(--line-control)] text-[var(--tx-pure)]"
                  } focus-within:outline focus-within:outline-2 focus-within:outline-offset-2`}
                >
                  <input
                    ref={i === 0 ? firstRadioRef : undefined}
                    type="radio"
                    name={`${uid}-kind`}
                    value={p.key}
                    checked={preset?.key === p.key}
                    onChange={() => setPreset(p)}
                    className="sr-only"
                  />
                  {p.label}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="mt-3 block text-[12px] text-[var(--tx-secondary)]">
            Note (optional)
            <input
              type="text"
              value={note}
              maxLength={200}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submit();
                }
              }}
              className={`mt-1 box-border block min-h-8 w-full min-w-0 rounded border border-[var(--line-control)] bg-transparent px-2 text-[13px] text-[var(--tx-pure)] ${FOCUS}`}
            />
          </label>
          {error && (
            <p
              role="alert"
              data-testid="log-contact-error"
              className="mt-2 text-[12px] text-[var(--state-warning)]"
            >
              {error}
            </p>
          )}
          <div className="mt-3 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={close}
              className={`min-h-8 rounded px-2 text-[12px] text-[var(--tx-secondary)] hover:text-[var(--tx-pure)] ${FOCUS}`}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!preset || busy}
              data-testid="log-contact-submit"
              className={`min-h-8 rounded bg-[var(--tx-pure)] px-3 text-[12px] font-semibold text-[var(--bz-base)] disabled:opacity-50 ${FOCUS}`}
            >
              {busy ? "Logging…" : "Log"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
