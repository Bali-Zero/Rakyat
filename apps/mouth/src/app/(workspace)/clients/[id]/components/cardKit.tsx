"use client";

import React, { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { SERIF_SECTION } from "@/components/workspace/r19/tokens";
import { spanValue } from "./overview-brief";

/**
 * The shared shape of the Overview's identity cards (client, passport, visa)
 * and the vault: one frame with its own tint, a decorative illustration, a
 * serif heading, label/value facts. Tints are token mixes (slate = info,
 * forest = success, neutral = the card ground) so each card is recognisable at
 * a glance without a new colour; copper never fills anything here.
 */

export type CardTone = "neutral" | "slate" | "forest";
export type ArtName = "client-info" | "passport" | "visa" | "vault-rings";

const FRAME: Record<CardTone, string> = {
  neutral: "border-[var(--bz-border)] bg-[var(--bz-card)]",
  slate:
    "border-[color-mix(in_srgb,var(--state-info)_32%,var(--bz-border))] bg-[color-mix(in_srgb,var(--state-info)_6%,var(--bz-card))]",
  forest:
    "border-[color-mix(in_srgb,var(--state-success)_32%,var(--bz-border))] bg-[color-mix(in_srgb,var(--state-success)_6%,var(--bz-card))]",
};

const KICKER: Record<CardTone, string> = {
  neutral: "text-[var(--tx-secondary)]",
  slate: "text-[var(--state-info)]",
  forest: "text-[var(--state-success)]",
};

const RULE: Record<CardTone, string> = {
  neutral: "border-t-[var(--tx-pure)]",
  slate: "border-t-[var(--state-info)]",
  forest: "border-t-[var(--state-success)]",
};

/**
 * A decorative illustration. It is `alt=""` and `aria-hidden` (it carries no
 * information), lazy-loaded, sized so it never shifts layout, and it removes
 * itself when the file is missing or fails to load.
 */
export function CardArt({
  name,
  size = 96,
  className,
  alt,
}: {
  name: ArtName;
  size?: number;
  className?: string;
  /** Only for a brand mark that should be announced; decorative art leaves it unset. */
  alt?: string;
}) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={`/images/kita/client-card/${name}.webp`}
      alt={alt ?? ""}
      aria-hidden={alt ? undefined : "true"}
      loading="lazy"
      decoding="async"
      width={size}
      height={size}
      onError={() => setFailed(true)}
      data-testid={`card-art-${name}`}
      className={cn(
        "pointer-events-none h-[104px] w-[104px] shrink-0 select-none object-contain object-right",
        className,
      )}
    />
  );
}

export function CardFrame({
  tone,
  kicker,
  title,
  art,
  artSize,
  testId,
  headerExtra,
  children,
  actions,
  className,
}: {
  tone: CardTone;
  kicker: string;
  title: string;
  art?: ArtName;
  artSize?: number;
  testId: string;
  /** Sits under the title, left of the illustration (a pill, a one-liner). */
  headerExtra?: React.ReactNode;
  children: React.ReactNode;
  /** The card's footer: primary actions bottom-left. */
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      aria-label={title}
      id={testId}
      tabIndex={-1}
      data-testid={testId}
      className={cn(
        "relative flex min-w-0 flex-col overflow-hidden rounded-xl border border-t-[3px] p-5 scroll-mt-28 focus:outline-none",
        FRAME[tone],
        RULE[tone],
        className,
      )}
    >
      {/* Header zone: text left, illustration right, fixed height. The
          fields start BELOW it, so art never sits on text or controls. */}
      <header
        data-testid={`${testId}-header`}
        className="flex h-[104px] items-start justify-between gap-3 overflow-hidden"
      >
        <div className="min-w-0 flex-1">
          <p className={cn("text-[12px] font-semibold", KICKER[tone])}>
            {kicker}
          </p>
          <h2
            className="mt-0.5 text-[22px] leading-[1.1] tracking-[-0.02em] md:whitespace-nowrap md:text-[28px]"
            style={SERIF_SECTION}
            data-testid={`${testId}-title`}
          >
            {title}
          </h2>
          {headerExtra ? <div className="mt-1.5">{headerExtra}</div> : null}
        </div>
        {art ? (
          <CardArt
            name={art}
            size={artSize ?? 104}
            className="w-auto min-w-0 max-w-[38%] shrink"
          />
        ) : null}
      </header>
      <div className="mt-4 flex-1">{children}</div>
      {actions ? (
        <footer className="mt-5 flex flex-wrap items-center gap-2 border-t border-[var(--bz-border)] pt-4">
          {actions}
        </footer>
      ) : null}
    </section>
  );
}

/** A label over a value; `wide` spans the whole row of the facts grid. */
export function Fact({
  label,
  children,
  wide,
  testId,
}: {
  label: string;
  children: React.ReactNode;
  wide?: boolean;
  testId?: string;
}) {
  return (
    <div className={cn("min-w-0", wide && "col-span-2")} data-testid={testId}>
      <dt className="text-[12px] text-[var(--tx-secondary)]">{label}</dt>
      <dd className="mt-0.5 min-w-0 break-words text-[14px] leading-[1.45] text-[var(--tx-pure)]">
        {children}
      </dd>
    </div>
  );
}

export const Facts = ({ children }: { children: React.ReactNode }) => (
  <dl className="grid grid-cols-2 gap-x-5 gap-y-3.5">{children}</dl>
);

export const NOT_RECORDED = (
  <span className="text-[13px] text-[var(--tx-secondary)]">Not recorded</span>
);

/** "62 days left" / "3y 10mo left" / "Expired 12 days ago" / "Expires today". */
export function remainingLabel(days: number | null): string | null {
  if (days === null) return null;
  if (days < 0) return `Expired ${-days} ${days === -1 ? "day" : "days"} ago`;
  if (days === 0) return "Expires today";
  const s = spanValue(days);
  return s.unit ? `${s.value} ${s.unit} left` : `${s.value} left`;
}

/** Same threshold the rest of the page uses for "soon": two months. */
export const SOON_DAYS = 60;

export type ExpiryTone = "ok" | "soon" | "gone";

export const expiryTone = (days: number | null): ExpiryTone =>
  days === null ? "ok" : days < 0 ? "gone" : days <= SOON_DAYS ? "soon" : "ok";

/** Words travel with the colour: the label already says expired / soon. */
export function ExpiryText({
  days,
  className,
}: {
  days: number | null;
  className?: string;
}) {
  const label = remainingLabel(days);
  if (!label) return null;
  const tone = expiryTone(days);
  return (
    <span
      className={cn(
        "text-[13px]",
        tone === "gone" && "font-bold text-[var(--state-warning)]",
        tone === "soon" && "font-semibold text-[var(--state-warning)]",
        tone === "ok" && "text-[var(--tx-secondary)]",
        className,
      )}
    >
      {label}
    </span>
  );
}

/**
 * The permit's issue->expiry window as a runway: the filled share is time
 * already used, the knob is today. It grows once on mount (the page's one
 * deliberate motion) and stays still under prefers-reduced-motion.
 */
export function Runway({ share, warn }: { share: number; warn: boolean }) {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    const id = requestAnimationFrame(() => setShown(share));
    return () => cancelAnimationFrame(id);
  }, [share]);
  const pct = Math.round(share * 100);
  const motion =
    "duration-700 ease-out motion-reduce:transition-none motion-reduce:duration-0";
  return (
    <span
      role="img"
      aria-label={`${pct}% of the permit's validity used`}
      data-testid="permit-runway"
      className="relative mt-1 block h-1.5 w-full rounded-full bg-[var(--bz-border)]"
    >
      <span
        className={cn(
          "absolute inset-y-0 left-0 block rounded-full transition-[width]",
          motion,
          warn ? "bg-[var(--state-warning)]" : "bg-[var(--tx-pure)]",
        )}
        style={{ width: `${shown * 100}%` }}
      />
      <span
        aria-hidden="true"
        className={cn(
          "absolute top-1/2 block h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 bg-[var(--bz-card)] transition-[left]",
          motion,
          warn ? "border-[var(--state-warning)]" : "border-[var(--tx-pure)]",
        )}
        style={{ left: `${shown * 100}%` }}
      />
    </span>
  );
}
