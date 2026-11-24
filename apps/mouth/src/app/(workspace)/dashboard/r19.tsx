"use client";

/**
 * The dashboard's R19 seam — a RE-EXPORT, not a copy.
 *
 * When PR #6483 shipped the Portal Champion widget (2026-09-14; the widget has
 * since been retired) the shared kita primitives did not exist yet, so
 * `r19.tsx` held a page-local copy of the idiom and said so in its own doc
 * block. SAETTA-R19K window K1b then shipped `@/components/workspace/r19` as
 * the one module every kita window imports, and the K2 brief makes this the
 * moment the copy goes: the dashboard files keep every import they already
 * had, and get them from the shared module instead.
 *
 * That is the whole change. `r19.test.tsx` — which scans THIS file and
 * `ChampionArena.tsx` for a `--state-danger` read or a literal hex, and
 * asserts `PILL_TONE` never touches danger — still binds, because the shared
 * tokens satisfy it: on kita `--state-danger` resolves to copper and the
 * module declares no red at all.
 *
 * The shared `PILL_TONE.you` reads `--bz-copper-text` where this copy read
 * `--bz-copper`; both are the copper seam and the `ink` tone the widget's
 * "Tax" chip needed is declared there too, promoted from here by K1b.
 *
 * Anything this file once defined and no dashboard file imports is gone
 * rather than re-exported: a symbol with no caller is how a second visual
 * system starts again.
 */

export { FOCUS, PILL_TONE, SERIF } from "@/components/workspace/r19";
