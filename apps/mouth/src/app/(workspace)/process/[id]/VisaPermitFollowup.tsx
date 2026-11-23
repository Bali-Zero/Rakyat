"use client";

import { useState } from "react";

export interface VisaFollowup {
  slot_key: string;
  started_on: string;
  due_on: string;
  final_due_on: string;
  days_remaining: number;
  status:
    | "awaiting_permit"
    | "overdue"
    | "escalation_due"
    | "email_sending"
    | "email_failed"
    | "email_uncertain"
    | "email_sent_permit_due"
    | "fulfilled";
  postponed_at: string | null;
  postponement_note: string | null;
  escalation_note: string | null;
  email_sent_at: string | null;
  fulfilled_at: string | null;
  can_postpone: boolean;
  can_escalate: boolean;
  can_confirm_receipt: boolean;
  email_receipt_confirmed_at: string | null;
}

export type FollowupAction = "postpone" | "escalate" | "confirm-receipt";
const buttonStyle =
  "mt-2 text-sm font-semibold text-[var(--bz-accent)] underline disabled:opacity-50";

export function VisaPermitFollowup({
  practiceId,
  value,
  busy,
  writable,
  act,
}: {
  practiceId: number;
  value: VisaFollowup;
  busy: boolean;
  writable: boolean;
  act: (action: FollowupAction, note: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState<FollowupAction | null>(null);
  const [note, setNote] = useState("");
  const allowed = (action: FollowupAction) =>
    writable &&
    {
      postpone: value.can_postpone,
      escalate: value.can_escalate,
      "confirm-receipt":
        value.status === "email_uncertain" && value.can_confirm_receipt,
    }[action];
  const validNote =
    draft === "confirm-receipt"
      ? note.trim().length >= 3 && note.trim().length <= 250
      : Boolean(note.trim());
  function prepare(action: FollowupAction) {
    setNote("");
    setDraft(action);
  }
  const dayText = `${value.days_remaining} ${value.days_remaining === 1 ? "day" : "days"} remaining`;
  return (
    <div
      className="mt-2 border-l-2 border-[var(--bz-accent)] pl-3 text-sm"
      aria-label="Permit follow-up"
    >
      <p>
        First qualifying upload ·{" "}
        <time dateTime={value.started_on}>{value.started_on}</time>
      </p>
      <p>
        Due <time dateTime={value.due_on}>{value.due_on}</time>
        {!value.fulfilled_at && ` · ${dayText}`}
      </p>
      {value.postponed_at && (
        <p>
          Client has not entered · deadline extended once to{" "}
          <time dateTime={value.final_due_on}>{value.final_due_on}</time>
        </p>
      )}
      {value.postponement_note && (
        <p className="break-words">Staff note · {value.postponement_note}</p>
      )}
      {value.status === "fulfilled" && <p>Required permit received.</p>}
      {value.status === "overdue" && (
        <p>
          Upload the permit, or record a postponement if the client has not
          entered and the option is available.
        </p>
      )}
      {value.status === "escalation_due" && (
        <p>Final deadline reached. Escalation to Zero is due.</p>
      )}
      {value.status === "email_sending" && (
        <p role="status">Email submission in progress.</p>
      )}
      {value.status === "email_failed" && (
        <p role="status">
          Email delivery failed. Review the escalation before retrying.
        </p>
      )}
      {value.status === "email_uncertain" && (
        <p role="status">
          Email outcome unconfirmed. Check the delivery record before any
          further action.
        </p>
      )}
      {value.email_sent_at && (
        <p role="status">
          Official email to Zero sent ·{" "}
          <time dateTime={value.email_sent_at}>
            {new Intl.DateTimeFormat("en-GB", {
              timeZone: "Asia/Makassar",
              dateStyle: "medium",
              timeStyle: "short",
            }).format(new Date(value.email_sent_at))}{" "}
            WITA
          </time>
        </p>
      )}
      {value.email_receipt_confirmed_at && (
        <p role="status">Email receipt confirmed by Zero.</p>
      )}
      {value.status === "email_sent_permit_due" && (
        <p>
          The bonus payment stays on hold until the required permit is uploaded.
        </p>
      )}
      {allowed("postpone") && draft !== "postpone" && (
        <button
          type="button"
          className={buttonStyle}
          disabled={busy}
          onClick={() => prepare("postpone")}
        >
          Client has not entered · postpone once
        </button>
      )}
      {allowed("escalate") && draft !== "escalate" && (
        <button
          type="button"
          className={buttonStyle}
          disabled={busy}
          onClick={() => prepare("escalate")}
        >
          {value.status === "email_failed"
            ? "Review and retry email to Zero"
            : "Prepare official email to Zero"}
        </button>
      )}
      {allowed("confirm-receipt") && draft !== "confirm-receipt" && (
        <button
          type="button"
          className={buttonStyle}
          disabled={busy}
          onClick={() => prepare("confirm-receipt")}
        >
          Record receipt of the existing email
        </button>
      )}
      {draft && allowed(draft) && (
        <form
          className="mt-2"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!validNote || busy || !allowed(draft)) return;
            await act(draft, note.trim());
          }}
        >
          {draft === "escalate" && (
            <p>Official email to Zero · case #{practiceId}</p>
          )}
          <label>
            {draft === "postpone"
              ? "Private staff note about the client’s entry"
              : draft === "confirm-receipt"
                ? "Mailbox Message-ID or Brevo reference for the received email"
                : "Note to Zero about the missing permit"}
            <textarea
              className="mt-1 block w-full rounded border p-2"
              required
              maxLength={draft === "confirm-receipt" ? 250 : 1000}
              value={note}
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          <button className={buttonStyle} disabled={!validNote || busy}>
            {draft === "postpone"
              ? `Confirm postponement to ${value.final_due_on}`
              : draft === "confirm-receipt"
                ? "Confirm existing email receipt"
                : "Confirm and send email to Zero"}
          </button>
          <button
            type="button"
            className="ml-3 underline"
            disabled={busy}
            onClick={() => {
              setDraft(null);
              setNote("");
            }}
          >
            Cancel
          </button>
        </form>
      )}
    </div>
  );
}
