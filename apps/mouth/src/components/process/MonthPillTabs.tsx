import { ChevronLeft, ChevronRight } from "lucide-react";

interface MonthPillTabsProps {
  selectedMonth: string;
  onMonthChange: (month: string) => void;
}

function shiftMonth(value: string, step: number) {
  const [year, month] = value.split("-").map(Number);
  const date = new Date(year, month - 1 + step, 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

export function MonthPillTabs({
  selectedMonth,
  onMonthChange,
}: MonthPillTabsProps) {
  const now = new Date();
  const currentMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const arrowClass =
    "grid size-11 shrink-0 place-items-center rounded-md text-[var(--bz-text-2)] hover:bg-[var(--bz-card-hover)] disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-[var(--bz-focus-ring)]";
  return (
    <div className="inline-flex items-center rounded-md border border-[var(--bz-border)] bg-[var(--bz-card)]">
      <button
        type="button"
        aria-label="Previous month"
        className={arrowClass}
        onClick={() => onMonthChange(shiftMonth(selectedMonth, -1))}
      >
        <ChevronLeft aria-hidden="true" className="size-4" />
      </button>
      <input
        aria-label="Process month"
        type="month"
        value={selectedMonth}
        max={currentMonth}
        onChange={(event) => {
          const month = event.target.value;
          if (/^\d{4}-(0[1-9]|1[0-2])$/.test(month) && month <= currentMonth)
            onMonthChange(month);
        }}
        className="min-h-11 max-w-40 bg-transparent px-1 text-sm font-semibold text-[var(--bz-text-1)]"
      />
      <button
        type="button"
        aria-label="Next month"
        className={arrowClass}
        disabled={selectedMonth >= currentMonth}
        onClick={() => onMonthChange(shiftMonth(selectedMonth, 1))}
      >
        <ChevronRight aria-hidden="true" className="size-4" />
      </button>
    </div>
  );
}
