import { Check } from "lucide-react";
import type { DoneTodayResponse } from "@/lib/api/workspace/accounting.api";

export type Situation = "confirm" | "link" | "choose" | "nomatch";
export type ReconciliationTab = Situation | "done";

export const SITUATIONS: Situation[] = ["confirm", "link", "choose", "nomatch"];

export const SITUATION_CLASS: Record<Situation, string> = {
  confirm: "cc-s-confirm",
  link: "cc-s-link",
  choose: "cc-s-choose",
  nomatch: "cc-s-nomatch",
};

const LABEL: Record<Situation, string> = {
  confirm: "To confirm",
  link: "To link",
  choose: "To choose",
  nomatch: "No match",
};

const TAB_BASE =
  "inline-flex h-[42px] items-center gap-2.5 rounded-[12px] px-4 text-base font-semibold";
const TAB_ACTIVE = "bg-[var(--cc-solid)] text-[var(--cc-on)]";
const TAB_EMPTY = "text-[var(--cc-tx-2)]";
const TAB_READY = "text-[var(--cc-tx)] hover:bg-[var(--cc-sel)]";
const BADGE_ACTIVE = "bg-[var(--cc-on)] text-[var(--cc-solid)]";
const BADGE_READY = "bg-[var(--cc-pill)] text-[var(--cc-tx)]";
const DONE_ACTIVE = "bg-[var(--cc-wash)] text-[var(--cc-tx)]";

export function ReconciliationTabs({
  active,
  counts,
  loaded,
  done,
  doneUnavailable,
  onSelect,
}: {
  active: ReconciliationTab | null;
  counts: Record<Situation, number>;
  loaded: boolean;
  done: DoneTodayResponse | null;
  doneUnavailable: boolean;
  onSelect: (tab: ReconciliationTab) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="Reconciliation"
      className="mt-[30px] flex items-center gap-1.5"
    >
      {SITUATIONS.map((key) => {
        const count = counts[key];
        const selected = active === key;
        const empty = loaded && count === 0;
        return (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={selected}
            data-testid={`cc-tab-${key}`}
            disabled={!loaded || empty}
            onClick={() => onSelect(key)}
            className={`${SITUATION_CLASS[key]} ${TAB_BASE} ${
              selected ? TAB_ACTIVE : empty ? TAB_EMPTY : TAB_READY
            }`}
          >
            {LABEL[key]}
            {!loaded ? null : empty ? (
              <Check className="h-4 w-4" aria-hidden="true" />
            ) : (
              <span
                data-testid={`cc-count-${key}`}
                className={`inline-flex h-6 min-w-6 items-center justify-center rounded-[12px] px-[7px] font-serif text-sm font-semibold tabular-nums ${
                  selected ? BADGE_ACTIVE : BADGE_READY
                }`}
              >
                {count}
              </span>
            )}
          </button>
        );
      })}
      <span
        aria-hidden="true"
        className="mx-1.5 h-[22px] w-px bg-[var(--cc-edge)]"
      />
      <span
        data-testid="cc-money-out"
        aria-disabled="true"
        className="inline-flex h-[42px] items-center gap-2.5 px-4 text-base font-medium text-[var(--cc-tx-2)]"
      >
        Money out <i className="text-xs not-italic">coming</i>
      </span>
      <span aria-hidden="true" className="flex-1" />
      {!doneUnavailable && (
        <button
          type="button"
          role="tab"
          aria-selected={active === "done"}
          data-testid="cc-tab-done"
          onClick={() => onSelect("done")}
          className={`${TAB_BASE} text-[var(--cc-tx-2)] ${active === "done" ? DONE_ACTIVE : ""}`}
        >
          Done today
          {done !== null && (
            <span
              data-testid="cc-count-done"
              className="font-serif text-sm font-semibold tabular-nums text-[var(--cc-tx)]"
            >
              {done.counts.total}
            </span>
          )}
        </button>
      )}
    </div>
  );
}
