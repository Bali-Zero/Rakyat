"use client";

import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Sparkles } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { FOCUS } from "@/components/workspace/r19";

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

/** "3 Nov" from an ISO timestamp (UTC), or null when it is not a date. */
function shortDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

type State =
  | { kind: "loading" }
  | { kind: "hidden" }
  | { kind: "ready"; text: string; generatedAt: string | null };

/**
 * A few plain-language lines about the tab, written by the backend from the
 * client's data (`GET /api/crm/clients/{id}/recap`). Fetched once per mount
 * (a tab view), never polled. Anything but a non-empty text — null text, a
 * 404 because the backend is not deployed yet, any error — renders nothing,
 * so the slot never shows a failure.
 */
export function AiRecap({
  clientId,
  scope,
  companyId,
  className,
}: {
  clientId: number;
  scope: "immigration" | "company";
  companyId?: number;
  className?: string;
}) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    void (async () => {
      try {
        const res = await api.crm.getClientRecap(clientId, scope, companyId);
        if (cancelled) return;
        const text = res?.text?.trim();
        setState(
          text
            ? { kind: "ready", text, generatedAt: res.generated_at }
            : { kind: "hidden" },
        );
      } catch {
        if (!cancelled) setState({ kind: "hidden" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [clientId, scope, companyId]);

  if (state.kind === "hidden") return null;

  if (state.kind === "loading") {
    return (
      <div
        data-testid="ai-recap-skeleton"
        aria-hidden="true"
        className={cn("flex flex-col gap-2", className)}
      >
        <span className="h-3 w-full rounded-[2px] bg-[var(--bz-border)] motion-safe:animate-pulse" />
        <span className="h-3 w-2/3 rounded-[2px] bg-[var(--bz-border)] motion-safe:animate-pulse" />
      </div>
    );
  }

  return (
    <RecapPanel
      text={state.text}
      generatedAt={state.generatedAt}
      className={className}
    />
  );
}

/** The full text; only a text taller than six lines is clamped, with a toggle. */
function RecapPanel({
  text,
  generatedAt,
  className,
}: {
  text: string;
  generatedAt: string | null;
  className?: string;
}) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el && !expanded) setOverflows(el.scrollHeight > el.clientHeight + 1);
  }, [text, expanded]);

  const when = shortDate(generatedAt);
  return (
    <aside
      aria-label="AI summary"
      className={cn(
        "flex min-w-0 flex-col gap-1.5 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-card)] px-4 py-3",
        className,
      )}
    >
      <span className="flex items-center gap-1.5 text-[11px] text-[var(--tx-secondary)]">
        <Sparkles className="h-3 w-3" aria-hidden="true" />
        AI summary
        {when ? <span>· Updated {when}</span> : null}
      </span>
      <p
        ref={ref}
        className={cn(
          "text-[13px] leading-snug text-[var(--tx-pure)]",
          !expanded && "line-clamp-6",
        )}
      >
        {text}
      </p>
      {overflows || expanded ? (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className={cn(
            "self-start text-[12px] text-[var(--tx-secondary)] underline underline-offset-4 hover:text-[var(--tx-pure)]",
            FOCUS,
          )}
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      ) : null}
    </aside>
  );
}
