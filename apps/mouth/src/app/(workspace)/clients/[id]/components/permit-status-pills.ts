// Owner decision 2026-11-03: in the Visa history the current permit's status
// reads in a light green tint and every completed (previous) permit's in a
// light red tint. On kita `--state-danger` resolves to the copper-rust tone
// (there is no true red in this system), so "light red" is a soft tint of it.
// This is the ONLY place under clients/[id] allowed to name `--state-danger`;
// the desk guard exempts this file by the COMPLETED_PILL identifier only.
export const CURRENT_PILL =
  "bg-[var(--state-success)]/10 text-[var(--state-success)]";
export const COMPLETED_PILL =
  "bg-[var(--state-danger)]/10 text-[var(--state-danger)]";
