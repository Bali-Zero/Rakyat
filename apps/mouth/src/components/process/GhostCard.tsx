import type { Practice } from "@/lib/api/crm/crm.types";
import { COLUMN_COLORS, getStatusColumn } from "./kanban-colors";

export function GhostCard({
  practice,
  onClick,
}: {
  practice: Practice;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="block min-h-11 w-full rounded-md px-2 py-2 text-left text-xs text-[var(--bz-text-2)] hover:bg-[var(--bz-card-hover)] focus-visible:outline-2 focus-visible:outline-[var(--bz-focus-ring)]"
    >
      <span className="block truncate font-medium text-[var(--bz-text-1)]">
        {practice.client_name || "Unknown Client"}
      </span>
      <span className="block truncate">
        {practice.practice_type_name ||
          practice.practice_type_code ||
          "Process"}{" "}
        · {COLUMN_COLORS[getStatusColumn(practice.status)].label}
      </span>
    </button>
  );
}
