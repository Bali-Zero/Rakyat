// apps/mouth/src/lib/workspace/roster-directory.ts
// ─────────────────────────────────────────────────────────────────────────────
// SERVER-ONLY directory data for the internal (workspace) pages.
//
// WHY THIS FILE EXISTS. Three workspace pages are `"use client"` and each held a
// hardcoded table of staff — a photo map keyed by email prefix, and a tax-consultant
// dropdown of email + display name. Those tables named people the owner had excluded
// from public pages, and a `"use client"` module's constants are shipped to the
// browser, so the names sat in three route chunks. Measured on the built output:
// `app/(workspace)/clients/[id]/page-*.js`, `.../lkpm/page-*.js` and
// `.../team-management/page-*.js`.
//
// The pages themselves are behind auth. The CHUNKS are not: a Next static asset is
// served from the public CDN path with no session, so
// `curl https://balizero.com/_next/static/chunks/app/(workspace)/lkpm/page-*.js`
// returned 200 and both names to an anonymous caller. "Internal page" does not cover
// a public asset — that distinction is the whole reason for this file.
//
// So the tables live here, on the server, and reach the pages as props.
// ─────────────────────────────────────────────────────────────────────────────

import { TEAM_ROSTER } from "@/data/team-roster";
import { error as logError } from "@/lib/utils/console";

/** email-prefix → portrait path, for the workspace people directory. */
export type TeamPhotoMap = Record<string, string>;

/**
 * Aliases the roster does not carry: email prefixes in use that are not roster slugs,
 * mapped to the portrait of the person they belong to. They cannot be derived, and they
 * name people, so they are supplied at runtime (server env `TEAM_EMAIL_PREFIX_ALIASES_JSON`,
 * a JSON object `{"prefix": "slug"}`), never committed. Unset or malformed = no aliases.
 */
function emailPrefixAliases(): Record<string, string> {
  const out: Record<string, string> = Object.create(null);
  out["dewa.ayu"] = "dewaayu";
  const raw = process.env.TEAM_EMAIL_PREFIX_ALIASES_JSON;
  if (!raw || !raw.trim()) return out;
  const reject = (why: string): Record<string, string> => {
    logError(`TEAM_EMAIL_PREFIX_ALIASES_JSON ignored: ${why}`);
    return out;
  };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return reject("not valid JSON");
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return reject("not a JSON object");
  }
  const slugs = new Set(TEAM_ROSTER.map((m) => m.slug));
  const extra: Record<string, string> = Object.create(null);
  for (const [alias, slug] of Object.entries(parsed)) {
    const key = alias.trim().toLowerCase();
    if (typeof slug !== "string" || !key) {
      return reject("a key or value is not usable");
    }
    const target = slug.trim().toLowerCase();
    if (!slugs.has(target)) return reject("a target is not a roster slug");
    extra[key] = target;
  }
  return Object.assign(out, extra);
}

/**
 * DERIVED from the roster, so a photo changes in one place.
 *
 * The map this replaces listed 16 entries by hand: 14 matched the roster exactly,
 * 0 disagreed, and 2 were the email aliases above. Deriving also picks up four
 * people who have a portrait in the roster but were missing from the hand-written
 * map (zainal, heru, ruslana, vino) — they now render their photo on the internal
 * directory instead of a blank. That is a deliberate consequence of having one
 * source of truth, not an accident, and it is declared in the PR body.
 */
export function teamPhotoMap(): TeamPhotoMap {
  const out: TeamPhotoMap = Object.create(null);
  for (const m of TEAM_ROSTER) {
    if (m.photo) out[m.slug] = m.photo;
  }
  for (const [alias, slug] of Object.entries(emailPrefixAliases())) {
    const photo = Object.prototype.hasOwnProperty.call(out, slug)
      ? out[slug]
      : undefined;
    if (photo) out[alias] = photo;
  }
  return out;
}

export interface TaxConsultantOption {
  value: string;
  label: string;
}

/**
 * The tax-consultant dropdown. NOT derived from the roster, deliberately.
 *
 * These `value`s are constrained by the backend: migration 093 carries a CHECK
 * constraint over exactly this set of addresses, and the form submits the value
 * verbatim. The roster's own emails DISAGREE with two of them — the roster has
 * `faysha.tax@…` (with a Y) where the constraint has `faisha.tax@…`, and
 * a second address whose local part differs the same way. Deriving the list
 * would therefore have silently changed what this form writes and broken the CHECK,
 * so the values are preserved here byte-for-byte and only MOVED off the client.
 *
 * The disagreement itself is real and is reported as a finding rather than papered
 * over: one of the two records is wrong, and which one is a question for the owner,
 * not for a presentation change.
 */
export const TAX_CONSULTANTS: readonly TaxConsultantOption[] = [
  // Owner roster 2026-11-03: Angel, Dewa Ayu, Kadek, Dea, Candra. Dea and
  // Candra keep plain emails (no `.tax@`); they are accepted by
  // migrations_v2/348 and the backend TAX_CONSULTANT_VALUES. A client already
  // holding a value that is no longer offered here keeps it (the selects render
  // the current value as an extra option).
  { value: "angel.tax@balizero.com", label: "Angel" },
  { value: "dewaayu.tax@balizero.com", label: "Dewa Ayu" },
  { value: "kadek.tax@balizero.com", label: "Kadek" },
  { value: "dea@balizero.com", label: "Dea" },
  { value: "candra@balizero.com", label: "Candra" },
  // Faisha (faisha.tax@ / faysha.tax@) offboarded 2026-09-19 — no longer an
  // assignable option. Existing clients/LKPM reports she is already on keep
  // that value (DB CHECK constraint from migration 093 is untouched), so
  // this list only gates NEW assignments.
];

/**
 * The LKPM assignee list. SEPARATE from TAX_CONSULTANTS on purpose: it gates
 * lkpm_reports.lkpm_assigned_to (DB CHECK lkpm_reports_assigned_to_check /
 * backend LKPM_ASSIGNEES), which is deliberately unchanged here. It is exactly
 * what TAX_CONSULTANTS held before Dea and Candra were added to the client
 * options.
 */
export const LKPM_ASSIGNEES: readonly TaxConsultantOption[] = [
  { value: "veronika.tax@balizero.com", label: "Veronika" },
  { value: "kadek.tax@balizero.com", label: "Kadek" },
  { value: "dewaayu.tax@balizero.com", label: "Dewa Ayu" },
  { value: "angel.tax@balizero.com", label: "Angel" },
];

/** A copy the client can hold: plain rows, no roster type crosses over. */
export function taxConsultants(): TaxConsultantOption[] {
  return TAX_CONSULTANTS.map((c) => ({ ...c }));
}

/** A copy of the LKPM assignee options (see LKPM_ASSIGNEES). */
export function lkpmAssignees(): TaxConsultantOption[] {
  return LKPM_ASSIGNEES.map((c) => ({ ...c }));
}
