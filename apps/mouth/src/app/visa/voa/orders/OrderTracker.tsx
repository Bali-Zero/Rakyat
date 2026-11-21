"use client";

import { useState } from "react";
import { AppFrame } from "@balizero/core";
import { formatIDR } from "@balizero/core/utils";
import { buildWhatsAppLink } from "@/lib/whatsapp-utm";
import { GarudaOrderError, resumeOrderCheckout } from "./api-client";
import { humanizePracticeKey, messageFor } from "./messages";
import {
  checkoutNavigation,
  isProviderCheckoutUrl,
} from "./provider-checkout-url";
import { useOrderTracking } from "./useOrderTracking";
import type { OrderView, PracticeState } from "./types";
import { VOA_PRIMARY_ACTION_STYLE } from "../voa-action-style";
import { useVoaLocale } from "../useVoaLocale";
import { voaCopy } from "../voa-copy";

/**
 * GARUDA VOA — order tracker + visa delivery view (`/visa/voa/orders/{orderId}`).
 *
 * "Parcel tracker" is the mandate's own word: a customer reads their state the way
 * they read a delivery-tracking page, ending in "delivered". There is no separate
 * artifact-download endpoint in the frozen contract (`OrderView`/`PracticeView` only
 * ever carry `artifact_available: boolean` — see openapi.yaml line ~1258: "no document
 * identifier or capability is placed in a URL"), so this single page IS the delivery
 * page once `practice.state === "Delivered"` rather than a second route pointed at a
 * read operation that doesn't exist. `DeliveredPanel` below is the addressable,
 * independently-tested unit that satisfies that build item.
 *
 * `order_state` from `getOrderAndPractice` is the ONLY thing this page ever treats as
 * authoritative for payment. `browser_observation` is rendered as a "confirming…"
 * hint at most — never as success (contract: "Browser-return observation is
 * non-authoritative; only signed webhook reconciliation can show paid").
 */
export function OrderTracker({ orderId }: { orderId: string }) {
  const { state, retry } = useOrderTracking(orderId);

  if (state.step === "loading") {
    return (
      <AppFrame
        funnel="visa"
        title="Your Visa on Arrival"
        subtitle="Checking your order…"
      >
        <p style={{ color: "var(--color-text-muted)" }}>One moment.</p>
      </AppFrame>
    );
  }

  if (state.step === "error") {
    return (
      <AppFrame
        funnel="visa"
        title="Your Visa on Arrival"
        subtitle="We couldn't load your order."
      >
        <p
          role="alert"
          style={{
            margin: 0,
            color: "var(--tx-pure)",
            borderLeft: "3px solid var(--bz-border)",
            paddingLeft: "0.75rem",
          }}
        >
          {state.message}
        </p>
        {state.retryable ? (
          <button
            type="button"
            onClick={retry}
            style={{
              padding: "0.9rem 1.4rem",
              borderRadius: 8,
              border: "none",
              ...VOA_PRIMARY_ACTION_STYLE,
              fontWeight: 600,
              cursor: "pointer",
              width: "fit-content",
            }}
          >
            Try again
          </button>
        ) : null}
        <WhatsAppHelp />
      </AppFrame>
    );
  }

  return <OrderTrackerReady order={state.order} />;
}

function OrderTrackerReady({ order }: { order: OrderView }) {
  // The journey's language, for the closed-order panel only: the rest of the
  // tracker stays as it is.
  const t = voaCopy(useVoaLocale());
  const subtitle = subtitleFor(order);

  return (
    <AppFrame
      funnel="visa"
      title="Your Visa on Arrival"
      subtitle={subtitle}
      footer="One all-inclusive price. Government fees, where they apply, are never billed separately from this figure."
    >
      <div
        style={{
          display: "grid",
          gap: "var(--space-2, 0.5rem)",
        }}
      >
        <span style={{ fontSize: "0.85rem", color: "var(--color-text-muted)" }}>
          {order.order_state === "paid" ? "Total paid" : "Order total"}
        </span>
        <span style={{ fontSize: "1.6rem", fontWeight: 600 }}>
          {formatIDR(order.price_idr)}
        </span>
      </div>

      {/*
       * The outcome, or the thing the customer has to DO, comes BEFORE the
       * seven-row rail — it used to come after it. A Blocked practice means we
       * are waiting on them, and burying that under seven progress rows is the
       * difference between a tracker they can read in five seconds and one
       * they have to work through. `PracticePanel` renders null in every state
       * that has neither an outcome nor an action, so this position costs
       * nothing in the ordinary case.
       */}
      {order.order_state === "paid" && order.practice ? (
        <PracticePanel practice={order.practice} />
      ) : null}

      <ParcelSteps order={order} />

      {order.order_state === "awaiting_payment" ||
      order.order_state === "created" ? (
        <AwaitingPaymentPanel
          orderId={order.order_id}
          canPay={order.order_state === "awaiting_payment"}
        />
      ) : null}

      {order.order_state === "failed" || order.order_state === "expired" ? (
        <ExceptionPanel
          tone="retry"
          heading={
            order.order_state === "failed"
              ? t("order.closed.failed.heading")
              : t("order.closed.expired.heading")
          }
          body={t("order.closed.safetyBody")}
          payAgainLabel={t("order.closed.payAgain")}
          payAgainResultId={order.result_id}
        />
      ) : null}

      {order.order_state === "refunded" ? (
        <ExceptionPanel
          tone="closed"
          heading="This order was refunded."
          body="If you still need a Visa on Arrival, a consultant can start a new application with you."
        />
      ) : null}

      {order.order_state === "paid" && order.practice === null ? (
        <p
          aria-live="polite"
          style={{ margin: 0, color: "var(--color-text-muted)" }}
        >
          Payment confirmed — setting up your application now.
        </p>
      ) : null}
    </AppFrame>
  );
}

function subtitleFor(order: OrderView): string {
  if (order.order_state === "created") {
    return "Setting up your payment — this takes a few seconds.";
  }
  if (order.order_state === "awaiting_payment") {
    return order.browser_observation === "browser_return_observed"
      ? "We're confirming your payment — this can take a minute."
      : "Complete your payment to start your application.";
  }
  if (order.order_state === "paid") {
    if (order.practice?.state === "Delivered") {
      return "Your visa has been delivered.";
    }
    return "Payment confirmed — here's where your application stands.";
  }
  if (order.order_state === "failed") return "Checkout couldn't be completed.";
  if (order.order_state === "expired") return "Checkout session expired.";
  if (order.order_state === "refunded") return "This order was refunded.";
  return "Tracking your Visa on Arrival application.";
}

const ORDER_STEP_ORDER: OrderView["order_state"][] = [
  "created",
  "awaiting_payment",
  "paid",
];

const PRACTICE_STEP_ORDER: PracticeState[] = [
  "Received",
  "In review",
  "Submitted",
  "Approved",
  "Delivered",
];

/** A simple ordered progress list — like a parcel's "Placed → Picked up → Out for
 * delivery → Delivered" — never inventing a state the backend didn't send. `Blocked`
 * and `Rejected` are exception branches rendered separately (`PracticePanel` below),
 * not points on this happy-path line. */
function ParcelSteps({ order }: { order: OrderView }) {
  const isException =
    order.order_state === "failed" ||
    order.order_state === "expired" ||
    order.order_state === "refunded";
  if (isException) return null;

  const orderStepIndex = ORDER_STEP_ORDER.indexOf(
    order.order_state as (typeof ORDER_STEP_ORDER)[number],
  );
  const practiceState = order.practice?.state;
  const practiceStepIndex =
    practiceState && practiceState !== "Blocked" && practiceState !== "Rejected"
      ? PRACTICE_STEP_ORDER.indexOf(practiceState)
      : -1;

  const steps: { label: string; done: boolean; current: boolean }[] = [
    {
      label: "Order placed",
      done: orderStepIndex >= 1,
      current: orderStepIndex === 0,
    },
    {
      label: "Payment confirmed",
      done: orderStepIndex >= 2,
      current: orderStepIndex === 1,
    },
    ...PRACTICE_STEP_ORDER.map((label, i) => ({
      label,
      done: orderStepIndex >= 2 && practiceStepIndex > i,
      current: orderStepIndex >= 2 && practiceStepIndex === i,
    })),
  ];

  return (
    <ol
      aria-label="Application progress"
      style={{
        display: "grid",
        gap: "var(--space-2, 0.5rem)",
        listStyle: "none",
        margin: 0,
        padding: 0,
      }}
    >
      {steps.map((step) => (
        <li
          key={step.label}
          aria-current={step.current ? "step" : undefined}
          style={stepStyle(step)}
        >
          <span aria-hidden="true">
            {step.done ? "✓" : step.current ? "●" : "○"}
          </span>
          <span>{step.label}</span>
        </li>
      ))}
    </ol>
  );
}

/**
 * Three tiers, three identities. `done` and `current` used to share
 * `--text-primary` — the glyph and the font weight were the only difference,
 * which is two states in one colour on the element whose entire job is telling
 * a customer which one of seven rows they are on.
 *
 * The order is deliberate and it is not "brighter as you progress": a finished
 * step RECEDES to `--tx-tertiary`, because it is no longer information anyone
 * is looking for. The current step is the only peak — `--bz-data` (this
 * theme's declared token for the one value on screen that matters) plus the
 * rule that makes it findable without reading. Future steps stay muted. So the
 * eye lands on one row out of seven, which is what "readable in five seconds"
 * has to mean on a list this long.
 *
 * `aria-current="step"` carries the same fact to a screen reader, which the
 * old font-weight never did.
 */
function stepStyle(step: {
  done: boolean;
  current: boolean;
}): React.CSSProperties {
  const base: React.CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: "0.6rem",
    padding: "0.3rem 0.5rem",
    borderLeft: "3px solid transparent",
    borderRadius: "0 4px 4px 0",
  };
  if (step.current) {
    return {
      ...base,
      color: "var(--bz-data)",
      fontWeight: 600,
      borderLeftColor: "var(--bz-data)",
      background: "var(--surface-raised)",
    };
  }
  if (step.done) {
    return { ...base, color: "var(--tx-tertiary)", fontWeight: 400 };
  }
  return { ...base, color: "var(--color-text-muted)", fontWeight: 400 };
}

/**
 * `getOrderAndPractice`'s `OrderView` carries no `checkout_url` — the read-only view never
 * holds a provider capability. "Pay now" (owner decision 2026-11-18) asks
 * `resumeOrderCheckout` for the order's ONE bound session at click time and leaves the
 * site only for an `https:` page on the provider's checkout host. Only `awaiting_payment`
 * gets the button: a `created` order has no session yet, and every other state has
 * nothing to pay. A refusal keeps the consultant link, which stays the fallback.
 */
function resumeErrorMessage(error: unknown): string {
  if (error instanceof GarudaOrderError) return messageFor(error.code);
  return "We couldn't open the payment page right now. Please try again in a moment.";
}

function AwaitingPaymentPanel({
  orderId,
  canPay,
}: {
  orderId: string;
  canPay: boolean;
}) {
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function payNow() {
    if (pending) return;
    setPending(true);
    setMessage(null);
    try {
      const { checkout_url } = await resumeOrderCheckout({ orderId });
      // Re-enabled after the hand-off too: a page restored from the
      // back/forward cache must not keep a dead "Opening payment…" button.
      if (isProviderCheckoutUrl(checkout_url)) {
        checkoutNavigation.assign(checkout_url);
      } else {
        setMessage(resumeErrorMessage(null));
      }
    } catch (error) {
      setMessage(resumeErrorMessage(error));
    }
    setPending(false);
  }

  return (
    <div style={{ display: "grid", gap: "var(--space-2, 0.5rem)" }}>
      {canPay ? (
        <button
          type="button"
          onClick={payNow}
          disabled={pending}
          aria-busy={pending}
          style={{
            padding: "0.9rem 1.4rem",
            borderRadius: 8,
            border: "none",
            ...VOA_PRIMARY_ACTION_STYLE,
            fontWeight: 600,
            cursor: pending ? "progress" : "pointer",
            width: "fit-content",
          }}
        >
          {pending ? "Opening payment…" : "Pay now"}
        </button>
      ) : null}
      {message ? (
        <p role="alert" style={{ margin: 0 }}>
          {message}
        </p>
      ) : null}
      <p style={{ margin: 0, color: "var(--color-text-muted)" }}>
        Haven&apos;t finished paying yet?{" "}
        <a
          href={buildWhatsAppLink(
            "visa",
            "Hi Bali Zero, I need help finishing payment for my Visa on Arrival order.",
          )}
          target="_blank"
          rel="noopener noreferrer"
        >
          A consultant can help you complete it →
        </a>
      </p>
    </div>
  );
}

function PracticePanel({
  practice,
}: {
  practice: NonNullable<OrderView["practice"]>;
}) {
  if (practice.state === "Delivered") {
    return <DeliveredPanel practice={practice} />;
  }
  if (practice.state === "Blocked") {
    return (
      <ExceptionPanel
        tone="needs-you"
        heading="We need something from you before we can continue."
        body={
          practice.required_action_key
            ? humanizePracticeKey(practice.required_action_key)
            : "A consultant can tell you exactly what's needed."
        }
      />
    );
  }
  if (practice.state === "Rejected") {
    return (
      <ExceptionPanel
        tone="refused"
        heading="Your application couldn't be approved as submitted."
        body={
          practice.customer_reason_key
            ? humanizePracticeKey(practice.customer_reason_key)
            : "A consultant can review it with you and explain the options."
        }
      />
    );
  }
  return null;
}

/** The "visa delivery page" build item. Contract has no bytes-download operation for
 * the finished document (`artifact_available` is a boolean flag only) — delivery
 * happens through the existing practice channels (email/WhatsApp), not a link this
 * page can construct itself, so this panel confirms the state honestly rather than
 * fabricating a download it cannot back. */
function DeliveredPanel({
  practice,
}: {
  practice: NonNullable<OrderView["practice"]>;
}) {
  return (
    // DELIBERATELY `--color-border-subtle`, and a boundary sweep must leave it
    // alone. This is a <section> container, not a control: SC 1.4.11 binds
    // user-interface components and graphical objects, and a card's hairline
    // outline is a divider. PR 6916 raised every CONTROL boundary on this
    // funnel to `--tx-tertiary`; raising this one too would make the panel
    // shout for a floor that does not apply to it. Same for ExceptionPanel.
    <section
      aria-label="Visa delivered"
      style={{
        display: "grid",
        gap: "var(--space-2, 0.6rem)",
        padding: "var(--space-3, 1rem)",
        borderRadius: 12,
        border: "1px solid var(--color-border-subtle)",
        background: "var(--surface-raised)",
      }}
    >
      <p style={{ margin: 0, fontWeight: 600 }}>
        Your Visa on Arrival has been delivered.
      </p>
      <p style={{ margin: 0, lineHeight: 1.6 }}>
        {practice.artifact_available
          ? "We've sent your document to the email on file. Check your inbox (and spam folder)."
          : "Our team is finalizing your document and will send it to your email shortly."}
      </p>
      <a
        href={buildWhatsAppLink(
          "visa",
          "Hi Bali Zero, I have a question about my delivered Visa on Arrival.",
        )}
        target="_blank"
        rel="noopener noreferrer"
        style={{
          display: "inline-block",
          width: "fit-content",
          padding: "0.7rem 1.1rem",
          borderRadius: 8,
          background: "var(--bz-green)",
          color: "var(--bz-base)",
          textDecoration: "none",
          fontWeight: 600,
        }}
      >
        Questions? Continue on WhatsApp →
      </a>
    </section>
  );
}

/**
 * Five situations a customer can land in — checkout failed, checkout expired,
 * order refunded, practice Blocked, practice Rejected — used to render behind
 * ONE `1px solid var(--color-border-subtle)`. Identical boxes, and the only
 * way to tell "we need a document from you" from "this was refunded" was to
 * read two paragraphs. The mandate's fourth accent is that every state has its
 * own visual identity; five states in one border is its exact inverse.
 *
 * The tones map to what the customer must DO, not to how bad the news is:
 *
 *   needs-you  copper --bz-accent      this theme's declared "needs you" tone;
 *                                      Blocked is the one state whose next
 *                                      move belongs to the customer
 *   refused    --bz-status-mark        Rejected. The one red on this surface,
 *                                      used as a rule and never as a fill or
 *                                      as text, per its globals.css contract
 *   retry      --state-warning         failed / expired: nothing is lost, the
 *                                      payment step simply has to happen again
 *   closed     --state-info            refunded: terminal, not an alarm
 *
 * `--state-danger` is unused here for the reason `SafeClock.tsx` records: on
 * this theme it resolves to the same value as `--bz-copper-text`, so it would
 * be indistinguishable from `needs-you`.
 */
export type ExceptionTone = "needs-you" | "refused" | "retry" | "closed";

export const EXCEPTION_RULE: Record<ExceptionTone, string> = {
  "needs-you": "var(--bz-accent)",
  refused: "var(--bz-status-mark)",
  retry: "var(--state-warning)",
  closed: "var(--state-info)",
};

/** Mirrors the contract's ResultId schema (`^[A-Za-z0-9_-]{22,128}$`). */
const RESULT_ID_PATTERN = /^[A-Za-z0-9_-]{22,128}$/;

function ExceptionPanel({
  heading,
  body,
  tone,
  payAgainResultId,
  payAgainLabel,
}: {
  heading: string;
  body: string;
  tone: ExceptionTone;
  /** Only the failed/expired panel passes this; a same-origin link, rendered only
   * when it matches the result-id pattern. */
  payAgainResultId?: string;
  /** The localised CTA text; passed with `payAgainResultId`. */
  payAgainLabel?: string;
}) {
  return (
    <section
      data-exception-tone={tone}
      style={{
        display: "grid",
        gap: "var(--space-2, 0.6rem)",
        padding: "var(--space-3, 1rem)",
        borderRadius: "0 12px 12px 0",
        border: "1px solid var(--color-border-subtle)",
        borderLeft: `4px solid ${EXCEPTION_RULE[tone]}`,
      }}
    >
      <p style={{ margin: 0, fontWeight: 600 }}>{heading}</p>
      <p style={{ margin: 0, lineHeight: 1.6 }}>{body}</p>
      {payAgainResultId !== undefined &&
      RESULT_ID_PATTERN.test(payAgainResultId) ? (
        <a
          href={`/visa/voa/checkout/${encodeURIComponent(payAgainResultId)}`}
          style={{
            display: "inline-block",
            width: "fit-content",
            padding: "0.7rem 1.1rem",
            borderRadius: 8,
            textDecoration: "none",
            fontWeight: 600,
            ...VOA_PRIMARY_ACTION_STYLE,
          }}
        >
          {payAgainLabel}
        </a>
      ) : null}
      <WhatsAppHelp />
    </section>
  );
}

function WhatsAppHelp() {
  return (
    <a
      href={buildWhatsAppLink(
        "visa",
        "Hi Bali Zero, I need help with my Visa on Arrival order.",
      )}
      target="_blank"
      rel="noopener noreferrer"
      style={{
        display: "inline-block",
        width: "fit-content",
        padding: "0.7rem 1.1rem",
        borderRadius: 8,
        background: "var(--bz-green)",
        color: "var(--bz-base)",
        textDecoration: "none",
        fontWeight: 600,
      }}
    >
      Continue on WhatsApp →
    </a>
  );
}
