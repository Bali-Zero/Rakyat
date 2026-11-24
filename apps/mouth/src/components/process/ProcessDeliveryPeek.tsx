"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  Check,
  ChevronDown,
  Circle,
  Files,
  Minus,
} from "lucide-react";
import { api } from "@/lib/api";
import { formatDate } from "@/lib/utils/format-date";
import {
  isUnconfiguredDelivery,
  type DeliverySlot,
} from "@/lib/api/crm/delivery-slot";
import {
  acceptResponse,
  isSameIdentity,
  readSessionIdentity,
  type SessionIdentity,
} from "@/lib/auth/session-identity";

type Props = { practiceId: number; identity: SessionIdentity };
type Checklist = {
  needs_titles: boolean;
  slots: DeliverySlot[];
  visa_followup?: {
    slot_key: string;
    due_on: string;
    fulfilled_at: string | null;
  } | null;
};

export function ProcessDeliveryPeek(props: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="group/pack border-t border-[var(--bz-border)] text-[var(--bz-text-1)]"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-sm text-xs font-semibold focus-visible:outline-2 focus-visible:outline-[var(--bz-focus-ring)] [&::-webkit-details-marker]:hidden">
        <Files className="size-4 shrink-0" aria-hidden />
        Delivery documents
        <ChevronDown
          className="ml-auto size-3.5 shrink-0 group-open/pack:rotate-180"
          aria-hidden
        />
      </summary>
      {open && <ChecklistContents {...props} />}
    </details>
  );
}

function ChecklistContents({ practiceId, identity }: Props) {
  const [data, setData] = useState<Checklist | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setData(null);
    setError(false);
    async function read() {
      try {
        if (!isSameIdentity(readSessionIdentity(), identity)) {
          setError(true);
          return;
        }
        const response = await api.requestWithCredential<Checklist>(
          `/api/crm/practices/${practiceId}/delivery`,
        );
        if (!active) return;
        if (acceptResponse(response, identity)) setData(response.data);
        else setError(true);
      } catch (cause) {
        if (!active) return;
        if (
          isSameIdentity(readSessionIdentity(), identity) &&
          isUnconfiguredDelivery(cause)
        ) {
          setData({ needs_titles: false, slots: [] });
        } else setError(true);
      }
    }
    void read();
    return () => {
      active = false;
    };
  }, [practiceId, identity, attempt]);

  if (error)
    return (
      <div className="pb-2 text-xs" role="alert">
        Documents unavailable.{" "}
        <button
          className="min-h-8 underline"
          onClick={() => setAttempt((value) => value + 1)}
        >
          Try again
        </button>
      </div>
    );
  if (!data)
    return (
      <p className="pb-3 text-xs text-[var(--bz-text-2)]" role="status">
        Loading documents…
      </p>
    );

  return (
    <div className="pb-2 text-xs">
      {data.needs_titles || !data.slots.length ? (
        <p className="pb-2 text-[var(--bz-text-2)]">
          {data.needs_titles
            ? "Document names still need to be agreed."
            : "No delivery checklist is configured for this service."}
        </p>
      ) : (
        <ul className="space-y-2">
          {data.slots.map((slot) => {
            const received = slot.document_id != null;
            const waived = Boolean(slot.condition && slot.waiver_reason);
            const followup = data.visa_followup;
            const due =
              !received &&
              !waived &&
              followup?.slot_key === slot.key &&
              !followup.fulfilled_at
                ? followup.due_on
                : null;
            const Icon = received ? Check : waived ? Minus : Circle;
            return (
              <li key={slot.key} className="flex items-start gap-2">
                <Icon
                  className={`mt-0.5 size-3.5 shrink-0 ${received ? "text-[var(--state-success)]" : "text-[var(--bz-text-2)]"}`}
                  aria-hidden
                />
                <div className="min-w-0">
                  <p className="break-words font-medium">{slot.label}</p>
                  <p className="text-[var(--bz-text-2)]">
                    {received
                      ? "Received"
                      : waived
                        ? "Not applicable"
                        : slot.condition
                          ? "To confirm"
                          : "Missing"}
                  </p>
                  {received && slot.file_name && (
                    <p className="break-all text-[var(--bz-text-2)]">
                      {slot.file_name}
                    </p>
                  )}
                  {!received && !waived && slot.condition && (
                    <p className="text-[var(--bz-text-2)]">{slot.condition}</p>
                  )}
                  {due && (
                    <p className="text-[var(--bz-text-1)]">
                      Due <time dateTime={due}>{formatDate(due)}</time>
                    </p>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {(data.needs_titles || data.slots.length > 0) && (
        <Link
          href={`/process/${practiceId}#final-delivery`}
          className="mt-1 inline-flex min-h-9 items-center gap-1 font-semibold underline-offset-4 hover:underline focus-visible:underline"
        >
          Open delivery <ArrowUpRight aria-hidden className="size-3.5" />
        </Link>
      )}
    </div>
  );
}
