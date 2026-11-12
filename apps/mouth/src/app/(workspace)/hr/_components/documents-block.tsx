"use client";
/**
 * The People console Documents block (STAFF_CARD.md C13): six papers, each present or missing with
 * the existing Drive "Open" link, and for `hr_card_write` viewers Upload / Replace and Remove.
 *
 * Personal data never reaches a DOM ATTRIBUTE: no person name, file name or file title is ever
 * given to an attribute (the hidden file input has only `type`, `accept` and `tabIndex`); the
 * picked file lives in the input's DOM property and is cleared after the read. Sending, and
 * binding the answer to the session, is the page's: this block only checks the size, asks the
 * parent, and shows its fixed message.
 */
import React, { useRef, useState } from "react";
import { ArrowUpRight, Check, FileText, Trash2, Upload } from "lucide-react";
import { DOC_MAX_BYTES, type DocSlot, type HrStaffCard } from "@/lib/api/hr/hr";
import {
  CARD,
  HAIR,
  INK,
  SOFT,
  TONE,
  UNVERIFIED,
  driveUrl,
  tint,
} from "./hr-panels";

export type DocResult = { ok: true } | { ok: false; message: string };

const ROWS: [DocSlot, string][] = [
  ["ktp", "ID card (KTP)"],
  ["kk", "Family card (KK)"],
  ["npwp", "Tax card (NPWP)"],
  ["bpjs_kes", "BPJS Kesehatan"],
  ["bpjs_tk", "BPJS Ketenagakerjaan"],
  ["contract", "Contract"],
];

export { UNVERIFIED };
export const DOC_TOO_LARGE =
  "That file is larger than 4 MB. Scan it at a lower resolution or compress it, then try again.";

/** The API never returns a stored value: `present` says a paper is on file, `file_id` is set only
 * when the door verified it. Only a verified id becomes a link. */
function stateOf(card: HrStaffCard, slot: DocSlot) {
  return card.documents?.[slot] ?? { present: false, file_id: null };
}

export function DocumentsBlock({
  card,
  canWrite,
  onUpload,
  onRemove,
}: {
  card: HrStaffCard;
  canWrite: boolean;
  onUpload: (slot: DocSlot, file: File) => Promise<DocResult>;
  onRemove: (slot: DocSlot) => Promise<DocResult>;
}) {
  const inputs = useRef<Record<string, HTMLInputElement | null>>({});
  const [busy, setBusy] = useState<DocSlot | null>(null);
  const [confirm, setConfirm] = useState<DocSlot | null>(null);
  const [notes, setNotes] = useState<Partial<Record<DocSlot, string>>>({});
  const tone = TONE.me;

  const note = (slot: DocSlot, text: string | null) =>
    setNotes((prev) => {
      const next = { ...prev };
      if (text) next[slot] = text;
      else delete next[slot];
      return next;
    });

  async function run(slot: DocSlot, job: () => Promise<DocResult>) {
    setBusy(slot);
    setConfirm(null);
    note(slot, null);
    try {
      const res = await job();
      if (!res.ok) note(slot, res.message);
    } catch {
      note(slot, "Could not save. Try again.");
    } finally {
      setBusy(null);
    }
  }

  function picked(slot: DocSlot) {
    const el = inputs.current[slot];
    const file = el?.files?.[0] ?? null;
    if (el) el.value = ""; // the file name must not linger in the DOM
    if (!file) return;
    if (file.size > DOC_MAX_BYTES) {
      note(slot, DOC_TOO_LARGE);
      return;
    }
    void run(slot, () => onUpload(slot, file));
  }

  return (
    <div
      className="mt-8 rounded-[24px] border p-5"
      style={{ borderColor: tint(tone, 30), background: CARD }}
    >
      <h3
        className={`flex items-center gap-2 text-[15px] font-semibold ${INK}`}
      >
        <FileText className="h-4 w-4" aria-hidden /> Documents
      </h3>
      <ul className={`mt-3 divide-y ${HAIR}`}>
        {ROWS.map(([slot, label]) => {
          const doc = stateOf(card, slot);
          const url = driveUrl(doc.file_id);
          const working = busy === slot;
          return (
            <li key={slot} className="flex flex-wrap items-center gap-3 py-3">
              <span className={`min-w-[10rem] text-[14px] font-medium ${INK}`}>
                {label}
              </span>
              {url ? (
                <span
                  className="inline-flex items-center gap-1 text-[13px] font-semibold"
                  style={{ color: tone }}
                >
                  <Check className="h-3.5 w-3.5" aria-hidden /> Present
                </span>
              ) : doc.present ? (
                <span className={`text-[13px] font-medium ${SOFT}`}>
                  {UNVERIFIED}
                </span>
              ) : (
                <span className={`text-[13px] ${SOFT}`}>Missing</span>
              )}
              {url && (
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  referrerPolicy="no-referrer"
                  className="inline-flex items-center gap-1 text-[13px] font-semibold"
                  style={{ color: tone }}
                >
                  Open <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
                </a>
              )}
              {canWrite && (
                <span className="ml-auto flex flex-wrap items-center gap-2">
                  <input
                    ref={(el) => {
                      inputs.current[slot] = el;
                    }}
                    type="file"
                    accept=".pdf,image/jpeg,image/png"
                    className="hidden"
                    tabIndex={-1}
                    onChange={() => picked(slot)}
                  />
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() => inputs.current[slot]?.click()}
                    className="inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[13px] font-semibold disabled:opacity-50"
                    style={{
                      color: tone,
                      border: `1.5px solid ${tint(tone, 55)}`,
                    }}
                  >
                    <Upload className="h-3.5 w-3.5" aria-hidden />
                    {working ? "Saving…" : doc.present ? "Replace" : "Upload"}
                  </button>
                  {doc.present &&
                    (confirm === slot ? (
                      <>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => void run(slot, () => onRemove(slot))}
                          className="rounded-full px-3.5 py-1.5 text-[13px] font-semibold text-white disabled:opacity-50"
                          style={{ background: "#dc2626" }}
                        >
                          Confirm remove
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirm(null)}
                          className={`rounded-full px-3 py-1.5 text-[13px] font-semibold ${SOFT}`}
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        disabled={busy !== null}
                        onClick={() => setConfirm(slot)}
                        className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[13px] font-semibold disabled:opacity-50 ${SOFT}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden /> Remove
                      </button>
                    ))}
                </span>
              )}
              {notes[slot] && (
                <p
                  role="alert"
                  className="w-full text-[13px] font-medium text-red-600"
                >
                  {notes[slot]}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
