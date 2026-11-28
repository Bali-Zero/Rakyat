"use client";

/**
 * Review — document requests (V2, second cut).
 *
 * A card reviewer sees the documents consultants asked outside a service card and decides each
 * one: add it to the card of that service (always, or only when a condition written in words
 * applies) or keep it for that case only. A decision can be reopened. The requests are already
 * with the clients; nothing here changes an open practice.
 *
 * The page is reached from the "Decide in Kita" link of the reviewers' email and by its address:
 * the existing /review page does not link here (a non-reviewer would cause a 403 on every load).
 */

import Link from "next/link";
import { useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api/error-handler";
import { useDocumentRequestReview } from "@/lib/hooks/useDocumentRequestReview";
import type {
  DocumentRequestDecisionBody,
  DocumentRequestReviewItem,
} from "@/lib/types/document-requests";
import { cn } from "@/lib/utils";
import {
  CARD,
  EmptyState,
  Eyebrow,
  FOCUS,
  Masthead,
  Notice,
  SERIF_SECTION,
  StatePill,
  TABULAR,
} from "@/components/workspace/r19";

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/** "28 Nov", from the ISO string, without a date library. */
function shortDate(iso: string | null): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? "");
  if (!match) return "";
  return `${Number(match[3])} ${MONTHS[Number(match[2]) - 1] ?? ""}`;
}

/** The part of an address before the "@", first letter in upper case. */
function nameOf(address: string | null): string {
  const local = (address ?? "").split("@")[0];
  return local ? local.charAt(0).toUpperCase() + local.slice(1) : "";
}

function ordinal(n: number): string {
  const lastTwo = n % 100;
  if (lastTwo >= 11 && lastTwo <= 13) return `${n}th`;
  switch (n % 10) {
    case 1:
      return `${n}st`;
    case 2:
      return `${n}nd`;
    case 3:
      return `${n}rd`;
    default:
      return `${n}th`;
  }
}

function seenText(seenCount: number): string {
  return seenCount === 0
    ? "First time"
    : `${ordinal(seenCount + 1)} time in 90 days`;
}

const REFUSALS: Record<string, string> = {
  already_decided: "Someone already decided this one.",
  already_on_card: "This document is already on the card.",
  no_card: "This service has no card.",
  not_decided: "This one is not decided.",
};

function refusalMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const code = (err.data as { detail?: { code?: unknown } } | undefined)
      ?.detail?.code;
    if (typeof code === "string" && REFUSALS[code]) return REFUSALS[code];
  }
  return "Could not save. Try again.";
}

const BUTTON =
  "inline-flex min-h-9 items-center justify-center rounded-md border px-4 text-sm font-medium disabled:opacity-50 " +
  FOCUS;
const PRIMARY = cn(
  BUTTON,
  "border-[var(--tx-pure)] bg-[var(--tx-pure)] text-[var(--bz-card)]",
);
const SECONDARY = cn(
  BUTTON,
  "border-[var(--line-control)] bg-transparent text-[var(--tx-pure)]",
);
const INPUT =
  "w-full rounded-md border border-[var(--line-control)] bg-transparent px-3 py-2 text-sm text-[var(--tx-pure)] focus:outline-none focus:border-[var(--bz-copper)]";

function PracticeLink({ id }: { id: number }) {
  return (
    <Link
      href={`/process/${id}`}
      className={cn("text-[12px] underline", TABULAR)}
    >
      {`#${id}`}
    </Link>
  );
}

function Who({ item }: { item: DocumentRequestReviewItem }) {
  return (
    <div className="min-w-0">
      <b className="block text-[14px] font-semibold">{item.label}</b>
      <span className="text-[12px] text-[var(--tx-secondary)]">
        {item.service_name ?? "No service"}
      </span>{" "}
      <PracticeLink id={item.practice_id} />
      <br />
      <span className="text-[12px] text-[var(--tx-secondary)]">
        {`${nameOf(item.requested_by)} · ${shortDate(item.requested_at)}`}
      </span>
    </div>
  );
}

function DecideBox({
  item,
  onDecide,
}: {
  item: DocumentRequestReviewItem;
  onDecide: (id: number, body: DocumentRequestDecisionBody) => Promise<unknown>;
}) {
  const [mode, setMode] = useState<"always" | "when">("always");
  const [when, setWhen] = useState("");
  const [clientText, setClientText] = useState(item.client_text);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const service = item.service_name ?? "";
  const canAdd =
    clientText.trim() !== "" && (mode === "always" || when.trim() !== "");

  const send = async (body: DocumentRequestDecisionBody) => {
    setBusy(true);
    setMessage(null);
    try {
      await onDecide(item.id, body);
    } catch (err) {
      setMessage(refusalMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const onlyThisCase = (
    <button
      type="button"
      className={SECONDARY}
      disabled={busy}
      onClick={() => void send({ decision: "case_only" })}
    >
      Only this case
    </button>
  );

  let body: ReactNode;
  if (item.card_state === "can_add") {
    const note =
      mode === "when"
        ? `It is saved on the ${service} card with its condition. It is not asked automatically. Open practices are not changed.`
        : item.card_live
          ? `New ${service} practices ask for it from now on. Open practices are not changed.`
          : `It is saved on the ${service} card. New practices will ask for it once that card is switched on. Open practices are not changed.`;
    body = (
      <>
        <h3 className="text-[16px] font-semibold">{`Add to the ${service} card`}</h3>
        <div className="mt-3 inline-flex overflow-hidden rounded-lg border border-[var(--line-control)]">
          {(
            [
              ["always", "Always"],
              ["when", "Only when…"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={mode === value}
              onClick={() => setMode(value)}
              className={cn(
                "px-4 py-2 text-sm",
                FOCUS,
                mode === value
                  ? "bg-[var(--bz-wash,transparent)] font-semibold text-[var(--tx-pure)] shadow-[inset_0_-3px_0_var(--tx-pure)]"
                  : "text-[var(--tx-secondary)]",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        {mode === "when" ? (
          <div className="mt-3.5">
            <label
              htmlFor={`when-${item.id}`}
              className="mb-1 block text-[12px] text-[var(--tx-secondary)]"
            >
              When
            </label>
            <input
              id={`when-${item.id}`}
              className={INPUT}
              value={when}
              required
              maxLength={500}
              onChange={(event) => setWhen(event.target.value)}
            />
          </div>
        ) : null}
        <div className="mt-3.5">
          <label
            htmlFor={`text-${item.id}`}
            className="mb-1 block text-[12px] text-[var(--tx-secondary)]"
          >
            What the client reads
          </label>
          <textarea
            id={`text-${item.id}`}
            className={INPUT}
            rows={3}
            required
            maxLength={2000}
            value={clientText}
            onChange={(event) => setClientText(event.target.value)}
          />
        </div>
        <div className="mt-4 flex items-center gap-2.5">
          <button
            type="button"
            className={PRIMARY}
            disabled={busy || !canAdd}
            onClick={() =>
              void send({
                decision: "added_to_card",
                condition: mode === "when" ? when.trim() : null,
                client_text: clientText.trim(),
              })
            }
          >
            Add to the card
          </button>
          {onlyThisCase}
        </div>
        <p className="mt-3 text-[12px] text-[var(--tx-secondary)]">{note}</p>
      </>
    );
  } else {
    const heading =
      item.card_state === "on_card"
        ? "Already on the card"
        : item.service_name
          ? "No card for this service"
          : "No service";
    const sentence =
      item.card_state === "on_card"
        ? `This document is already on the ${service} card.`
        : item.service_name
          ? "This service has no card to add to."
          : "This practice has no service, so there is no card to add to.";
    body = (
      <>
        <h3 className="text-[16px] font-semibold">{heading}</h3>
        <p className="mt-1 text-[14px]">{sentence}</p>
        <div className="mt-4">{onlyThisCase}</div>
      </>
    );
  }

  return (
    <div className="col-span-full mt-0.5 rounded-lg border border-[var(--line-control)] px-5 py-4">
      {body}
      {message ? (
        <Notice tone="you" role="alert" className="mt-3">
          {message}
        </Notice>
      ) : null}
    </div>
  );
}

function PendingCard({
  item,
  onDecide,
}: {
  item: DocumentRequestReviewItem;
  onDecide: (id: number, body: DocumentRequestDecisionBody) => Promise<unknown>;
}) {
  return (
    <article className="grid grid-cols-1 items-start gap-4 border-t border-[var(--bz-border)] py-4 first:border-t-0 md:grid-cols-[minmax(0,1.3fr)_minmax(0,1.5fr)]">
      <Who item={item} />
      <div>
        <StatePill
          tone={item.seen_count === 0 ? "wait" : "you"}
          label={seenText(item.seen_count)}
        />
        <p className="mt-1.5 text-[14px]">{item.reason}</p>
      </div>
      <DecideBox item={item} onDecide={onDecide} />
    </article>
  );
}

function outcome(item: DocumentRequestReviewItem): string {
  if (item.decision === "case_only") return "Only this case";
  return item.decision_condition
    ? `Added to the card, only when ${item.decision_condition}`
    : "Added to the card";
}

function DecidedLine({
  item,
  onReopen,
}: {
  item: DocumentRequestReviewItem;
  onReopen: (id: number) => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const reopen = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await onReopen(item.id);
    } catch (err) {
      setMessage(refusalMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="grid grid-cols-1 items-start gap-3 border-t border-[var(--bz-border)] py-3 first:border-t-0 md:grid-cols-[minmax(0,1.3fr)_minmax(0,1.5fr)_auto]">
      <Who item={item} />
      <div className="text-[14px]">
        <p>{outcome(item)}</p>
        <p className="text-[12px] text-[var(--tx-secondary)]">
          {`${nameOf(item.decided_by)} · ${shortDate(item.decided_at)}`}
        </p>
        {message ? <p role="alert">{message}</p> : null}
      </div>
      <button
        type="button"
        className={SECONDARY}
        disabled={busy}
        onClick={() => void reopen()}
      >
        Reopen
      </button>
    </li>
  );
}

export default function DocumentRequestReviewPage() {
  const { pending, decided, forbidden, isLoading, error, decide, reopen } =
    useDocumentRequestReview();

  if (forbidden) {
    return (
      <div className="mx-auto max-w-[1100px] py-8">
        <p className="text-[14px]">Only card reviewers see this page.</p>
      </div>
    );
  }

  const total = pending?.total ?? 0;
  const items = pending?.items ?? [];

  return (
    <div className="mx-auto max-w-[1100px]">
      <Masthead
        title="Review — document requests"
        sub="Only card reviewers see this page. The requests are already with the clients: here you decide what future practices ask for."
      />
      {error ? (
        <Notice tone="you" role="alert" className="mb-4">
          {error}
        </Notice>
      ) : null}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_280px]">
        <div className="space-y-8">
          <section className={cn(CARD, "p-5")}>
            <div className="mb-3 flex items-start justify-between gap-3">
              <div>
                <h2
                  className="text-[20px] tracking-[-0.02em]"
                  style={SERIF_SECTION}
                >
                  Asked outside the card
                </h2>
                <p className="text-[12px] text-[var(--tx-secondary)]">
                  {`${total} ${total === 1 ? "request" : "requests"}, newest first`}
                </p>
              </div>
              <StatePill tone="wait" label={`${total} to decide`} />
            </div>
            {isLoading ? (
              <p className="text-[14px]">Loading…</p>
            ) : items.length === 0 ? (
              <EmptyState>Nothing to decide.</EmptyState>
            ) : (
              items.map((item) => (
                <PendingCard key={item.id} item={item} onDecide={decide} />
              ))
            )}
          </section>

          {decided && decided.items.length > 0 ? (
            <section className={cn(CARD, "p-5")}>
              <h2
                className="mb-3 text-[20px] tracking-[-0.02em]"
                style={SERIF_SECTION}
              >
                Decided
              </h2>
              <ul>
                {decided.items.map((item) => (
                  <DecidedLine key={item.id} item={item} onReopen={reopen} />
                ))}
              </ul>
            </section>
          ) : null}
        </div>

        <aside>
          <section className={cn(CARD, "p-5")}>
            <Eyebrow>The rules</Eyebrow>
            <div className="mt-2 space-y-2 text-[14px]">
              <p>
                The request is already in the client&apos;s portal. This page
                never removes it.
              </p>
              <p>Adding to a card changes new practices only.</p>
              <p>
                Every decision is recorded with your name and the date.
                Reopening one clears it.
              </p>
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}
