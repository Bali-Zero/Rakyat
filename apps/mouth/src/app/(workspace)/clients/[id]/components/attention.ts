import type { AttentionItem, ClientDocument } from "@/lib/api/crm/crm.types";
import { spanValue, witaDateKey } from "./overview-brief";
import { permitLine } from "./header-facts";
import { permitDisplay } from "./permit-label";
import { documentValidity } from "./documentValidity";
import { pickCurrentVisa } from "./pickCurrentVisa";

/**
 * Wording and tone for the "Needs attention" ledger rows the backend computes
 * (`ClientProfile.attention`). Pure: the same item and clock always give the
 * same sentence, and colour is only ever a second channel — every state has a
 * word (`stateWord`) next to it.
 */

export type AttentionTone = "strong" | "warning" | "info" | "success" | "calm";

const TONE: Record<AttentionItem["state"], AttentionTone> = {
  expired: "strong",
  overdue: "strong",
  urgent: "warning",
  open: "warning",
  upcoming: "info",
  done: "success",
};

const WORD: Record<AttentionItem["state"], string> = {
  expired: "Expired",
  overdue: "Overdue",
  urgent: "Urgent",
  open: "Open",
  upcoming: "Upcoming",
  done: "Done",
};

export const attentionTone = (item: AttentionItem): AttentionTone =>
  TONE[item.state];
export const stateWord = (item: AttentionItem): string => WORD[item.state];

/** Worst first: expired, overdue, urgent, open, upcoming, done. */
const RANK: Record<AttentionItem["state"], number> = {
  expired: 0,
  overdue: 1,
  urgent: 2,
  open: 3,
  upcoming: 4,
  done: 5,
};

export function sortAttention(items: AttentionItem[]): AttentionItem[] {
  return items
    .map((item, i) => ({ item, i }))
    .sort(
      (a, b) =>
        RANK[a.item.state] - RANK[b.item.state] ||
        (a.item.days ?? Number.MAX_SAFE_INTEGER) -
          (b.item.days ?? Number.MAX_SAFE_INTEGER) ||
        a.i - b.i,
    )
    .map((x) => x.item);
}

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

/** "14 Nov", or "14 Nov 2027" when the year is not the current one (WITA). */
export function shortDate(date: string | null, now: number): string | null {
  const m = date?.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  const month = MONTHS[Number(m[2]) - 1];
  if (!month) return null;
  const sameYear = witaDateKey(now).startsWith(m[1]);
  return `${Number(m[3])} ${month}${sameYear ? "" : ` ${m[1]}`}`;
}

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? "" : "s"}`;

/** "in 62 days" / "today" / "in 1 day". */
/** "4 Jan 2027": always with the year, so a date never reads ambiguously. */
export function fullDate(date: string | null): string | null {
  const m = date?.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  const month = MONTHS[Number(m[2]) - 1];
  return month ? `${Number(m[3])} ${month} ${m[1]}` : null;
}

/**
 * The one sentence of a ledger row, per kind x state:
 *   permit / passport  expires <date> · N days | expired <date> · N days ago
 *   lkpm               opens <date> · in N days | window closes <date> | filed | overdue since <date>
 *   spt_*              due <date> · N days | filed | overdue since <date>
 * A permit's label is `permitDisplay(code, label)` ("E28A · Investor KITAS").
 * A family member's name leads the text, a company's name follows it.
 */
export function attentionText(item: AttentionItem, now: number): string {
  const { state, days, note, kind } = item;
  const date = fullDate(item.date);
  const opens = fullDate(item.opens_on);
  const label =
    kind === "permit"
      ? permitDisplay(item.code, item.label) || item.label
      : item.label;
  const n = days === null ? null : Math.abs(days);
  const away = n === null ? "" : ` · ${plural(n, "day")}`;
  let text: string;

  if (kind === "lkpm") {
    if (state === "done") text = `${label} filed`;
    else if (state === "overdue" || state === "expired")
      text = date ? `${label} overdue since ${date}` : `${label} overdue`;
    else if (state === "upcoming")
      text = `${label} opens ${opens ?? "soon"}${
        days !== null && days >= 0 ? ` · in ${plural(days, "day")}` : ""
      }`;
    else
      text = date
        ? `${label} window closes ${date}`
        : `${label} window is open`;
  } else if (kind === "spt_personal" || kind === "spt_company") {
    if (state === "done") text = `${label} filed`;
    else if (state === "overdue" || state === "expired")
      text = date ? `${label} overdue since ${date}` : `${label} overdue`;
    else
      text = `${label} due ${date ?? "soon"}${days !== null && days >= 0 ? away : ""}`;
  } else if (state === "done") {
    text = `${label} valid`;
  } else if (state === "expired" || state === "overdue") {
    text =
      `${label} expired ${date ?? ""}${n === null ? "" : ` · ${plural(n, "day")} ago`}`
        .replace(/\s+/g, " ")
        .trim();
  } else {
    text = `${label} expires ${date ?? "soon"}${days !== null && days >= 0 ? away : ""}`;
  }

  if (note === "itk_missing") text += " · ITK missing";
  if (item.subject_name) {
    if (item.subject === "family_member")
      text = `${item.subject_name} · ${text}`;
    else if (item.subject === "company") text += ` · ${item.subject_name}`;
  }
  return text;
}

// ---------------------------------------------------------------------------
// The ledger: ONE row per area (Visa, Company, Tax), the rest behind "Show N
// more". Owner 2026-11-03: "one thing per area, max 3 rows".
// ---------------------------------------------------------------------------

/** Where a row's "Review" action goes: a card on the Overview or another tab. */
export type ReviewRoute =
  | { card: "visa-card" | "passport-card" }
  | { tab: "family" | "company" | "tax" | "documents" };

/** v4 kinds to a destination. A family member's item never opens the client's own card. */
export function reviewRouteOf(item: AttentionItem): ReviewRoute {
  if (
    (item.kind === "permit" || item.kind === "passport") &&
    item.subject === "client"
  )
    return { card: item.kind === "permit" ? "visa-card" : "passport-card" };
  if (item.subject === "family_member") return { tab: "family" };
  if (item.kind === "lkpm") return { tab: "company" };
  if (item.kind === "spt_personal" || item.kind === "spt_company")
    return { tab: "tax" };
  return { tab: "documents" };
}

export interface LedgerRow {
  key: string;
  /** "Visa" | "Company" | "Tax" for the headline rows, null for the rest. */
  area: "Visa" | "Company" | "Tax" | "Passport" | "Family";
  /** One of the (max) three rows kept above the "Show N more" details. */
  headline: boolean;
  title: string;
  word: string;
  tone: AttentionTone;
  route: ReviewRoute;
}

/** Lower is worse; only used to decide who fills the visible slots. */
const SEVERITY: Record<AttentionTone, number> = {
  strong: 0,
  warning: 1,
  info: 2,
  calm: 3,
  success: 3,
};

/** The ledger shows at most this many rows before "Show N more". */
export const MAX_VISIBLE = 3;

export interface Ledger {
  rows: LedgerRow[];
  more: LedgerRow[];
}

/** The area label of an item that is not one of the headline rows. */
function areaOf(item: AttentionItem): LedgerRow["area"] {
  if (item.kind === "lkpm") return "Company";
  if (item.kind.startsWith("spt")) return "Tax";
  if (item.subject === "family_member") return "Family";
  if (item.subject === "company") return "Company";
  if (item.kind === "passport") return "Passport";
  return "Visa";
}

const rowOf = (
  item: AttentionItem,
  area: LedgerRow["area"],
  key: string,
  now: number,
  headline = true,
): LedgerRow => ({
  key,
  area,
  headline,
  title: attentionText(item, now),
  word: stateWord(item),
  tone: attentionTone(item),
  route: reviewRouteOf(item),
});

/** The permit row built from the current permit document alone. */
function visaRowFromDocument(
  documents: ClientDocument[],
  now: number,
): LedgerRow | null {
  const doc = pickCurrentVisa(documents);
  const line = permitLine(documents);
  if (!doc || !line) return null;
  const validity = documentValidity(doc, witaDateKey(now));
  const days = validity.daysLeft;
  const until = shortDate(validity.expiry, now);
  if (days === null || !until) {
    return {
      key: "visa-doc",
      area: "Visa",
      headline: true,
      route: { card: "visa-card" },
      title: `${line} — expiry date not recorded`,
      word: "No date",
      tone: "warning",
    };
  }
  if (validity.state === "expired") {
    return {
      key: "visa-doc",
      area: "Visa",
      headline: true,
      route: { card: "visa-card" },
      title: `${line} — expired ${plural(-days, "day")} ago`,
      word: "Expired",
      tone: "strong",
    };
  }
  const span = spanValue(days);
  const left = span.unit ? `${span.value} ${span.unit}` : span.value;
  return {
    key: "visa-doc",
    area: "Visa",
    headline: true,
    route: { card: "visa-card" },
    title: `${line} — valid until ${until} · ${left}`,
    word: validity.state === "expiring" ? "Expiring" : "Valid",
    tone: validity.state === "expiring" ? "warning" : "calm",
  };
}

/**
 * Builds the ledger. With `attention` absent only the Visa row exists, built
 * from the current permit document; the old view-based alert list is gone.
 */
export function buildLedger(
  attention: AttentionItem[] | undefined,
  documents: ClientDocument[],
  now: number,
): Ledger {
  const items = attention ? sortAttention(attention) : [];
  const used = new Set<AttentionItem>();
  const rows: LedgerRow[] = [];

  const ownPermit = items.find(
    (i) => i.kind === "permit" && i.subject === "client",
  );
  if (ownPermit) {
    used.add(ownPermit);
    rows.push(rowOf(ownPermit, "Visa", "visa", now));
  } else {
    const fromDoc = visaRowFromDocument(documents, now);
    if (fromDoc) rows.push(fromDoc);
  }

  const company = items.find((i) => i.kind === "lkpm");
  if (company) {
    used.add(company);
    rows.push(rowOf(company, "Company", "company", now));
  }
  const tax = items.find(
    (i) => i.kind === "spt_personal" || i.kind === "spt_company",
  );
  if (tax) {
    used.add(tax);
    rows.push(rowOf(tax, "Tax", "tax", now));
  }

  // Up to MAX_VISIBLE rows are shown, but severity wins over area: every
  // expired or overdue row is always visible (even past MAX_VISIBLE), and the
  // free slots go to the worst remaining rows, a headline row beating an
  // equally severe other one. A "done" item that is not a headline never takes
  // a slot. Whatever is not shown waits behind "Show N more".
  const candidates = [
    ...rows.map((row, pos) => ({ row, pos, headline: true, i: -1 })),
    ...items
      .map((item, i) => ({ item, i }))
      .filter(({ item }) => !used.has(item))
      .map(({ item, i }, k) => ({
        row: rowOf(item, areaOf(item), `more-${i}`, now, false),
        pos: rows.length + k,
        headline: false,
        i,
      })),
  ];
  const severity = (r: LedgerRow) => SEVERITY[r.tone];
  const visible = new Set(
    candidates.filter((c) => severity(c.row) === 0).map((c) => c.pos),
  );
  [...candidates]
    .filter(
      (c) => !visible.has(c.pos) && (c.headline || c.row.tone !== "success"),
    )
    .sort((a, b) => severity(a.row) - severity(b.row) || a.pos - b.pos)
    .forEach((c) => {
      if (visible.size < MAX_VISIBLE) visible.add(c.pos);
    });
  const shown: LedgerRow[] = [];
  const more: LedgerRow[] = [];
  for (const c of candidates) {
    if (visible.has(c.pos)) {
      shown.push(
        c.headline ? c.row : { ...c.row, key: `fill-${c.i}`, headline: true },
      );
    } else {
      more.push({ ...c.row, headline: false });
    }
  }
  return { rows: shown, more };
}
