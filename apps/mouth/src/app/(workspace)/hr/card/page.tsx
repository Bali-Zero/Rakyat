"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Banknote,
  FileText,
  FolderOpen,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { getMyCard, type HrStaffCard } from "@/lib/api/hr/hr";
import { DESK_PILL, DESK_RULE, DESK_SERIF } from "@balizero/core";

/**
 * "My card" — a staff member's own HR record, in full.
 *
 * The values are the member's own and are shown unmasked on purpose (owner
 * decision). They are rendered as text only and never leave this component:
 * not to the console, the URL, the document title or any query key.
 */

const INK = "text-[var(--tx-pure,var(--bz-text-1))]";
const HAIR = "border-[var(--bz-border)]";
const LABEL =
  "text-[10px] font-[750] uppercase tracking-[0.08em] text-[var(--tx-secondary,var(--bz-text-2))]";
const QUIET = "text-[var(--bz-text-3)]";
const NOT_ON_FILE = "Not on file yet";
const NOT_ON_FILE_HR = "Not on file yet — HR will add it";

// ─── Formatting ───────────────────────────────────────────────────────

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})/;

function parseDate(value: string | null | undefined): Date | null {
  const m = value ? DATE_ONLY.exec(value) : null;
  if (!m) return null;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatDay(value: string | null | undefined): string | null {
  const d = parseDate(value);
  return d
    ? d.toLocaleDateString("en-GB", {
        day: "numeric",
        month: "long",
        year: "numeric",
        timeZone: "UTC",
      })
    : null;
}

function formatMonthYear(value: string | null | undefined): string | null {
  const d = parseDate(value);
  return d
    ? d.toLocaleDateString("en-GB", {
        month: "long",
        year: "numeric",
        timeZone: "UTC",
      })
    : null;
}

function text(value: string | null | undefined): string | null {
  const t = typeof value === "string" ? value.trim() : "";
  return t ? t : null;
}

const FILE_ID = /^[A-Za-z0-9_-]{10,200}$/;

/** A Drive link only for an id of the expected shape; anything else is "not on file". */
function driveUrl(fileId: string | null | undefined): string | null {
  return typeof fileId === "string" && FILE_ID.test(fileId)
    ? `https://drive.google.com/file/d/${encodeURIComponent(fileId)}/view`
    : null;
}

function humanise(slot: string): string {
  const s = slot.replace(/[_-]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : slot;
}

// ─── Tiles ────────────────────────────────────────────────────────────

type TileId = "who" | "contract" | "tax" | "bank" | "docs";

interface Fact {
  label: string;
  value: string | null;
  href?: string | null;
  linkLabel?: string;
  multiline?: boolean;
  note?: string;
  /** Not counted towards "n of m on file" (e.g. an end date that may not exist). */
  informational?: boolean;
  emptyText?: string;
}

interface TileDef {
  id: TileId;
  title: string;
  summary: string;
  icon: LucideIcon;
  intro: string;
  facts: Fact[];
}

const KNOWN_SLOTS: [string, string][] = [
  ["contract", "Contract"],
  ["ktp", "ID card (KTP)"],
  ["kk", "Family card (KK)"],
  ["npwp", "Tax card (NPWP)"],
  ["bpjs_kes", "BPJS Kesehatan card"],
  ["bpjs_tk", "BPJS Ketenagakerjaan card"],
];

function buildTiles(card: HrStaffCard): TileDef[] {
  const docs = card.docs ?? {};
  const contractFile = driveUrl(card.contract?.file_id);

  const slotFacts: Fact[] = KNOWN_SLOTS.map(([slot, label]) => {
    const id =
      slot === "contract"
        ? (docs.contract ?? card.contract?.file_id)
        : docs[slot];
    const href = driveUrl(id);
    return { label, value: href ? "Open" : null, href, linkLabel: "Open" };
  });
  const known = new Set(KNOWN_SLOTS.map(([s]) => s));
  for (const slot of Object.keys(docs)) {
    if (known.has(slot)) continue;
    const href = driveUrl(docs[slot]);
    slotFacts.push({
      label: humanise(slot),
      value: href ? "Open" : null,
      href,
      linkLabel: "Open",
    });
  }

  return [
    {
      id: "who",
      title: "Who I am",
      summary: "Your legal name, ID number and personal details.",
      icon: UserRound,
      intro:
        "These are the personal details HR keeps on file for you, as they appear on your identity papers. Only you and HR can see this.",
      facts: [
        { label: "Legal name", value: text(card.legal_name) },
        { label: "NIK (ID number)", value: text(card.nik) },
        { label: "Place of birth", value: text(card.birth_place) },
        { label: "Date of birth", value: formatDay(card.birth_date) },
        { label: "Gender", value: text(card.gender) },
        { label: "Marital status", value: text(card.marital_status) },
        { label: "Religion", value: text(card.religion) },
        { label: "Address", value: text(card.address), multiline: true },
      ],
    },
    {
      id: "contract",
      title: "My contract",
      summary: "Who employs you, since when, and your contract.",
      icon: FileText,
      intro:
        "The terms under which you work with us. Only you and HR can see this.",
      facts: [
        { label: "Employer", value: text(card.employer_entity) },
        { label: "Unit", value: text(card.unit) },
        { label: "Start date", value: formatDay(card.hire_date) },
        {
          label: "End date",
          value: formatDay(card.end_date),
          informational: true,
          emptyText: "No end date — your employment is open-ended",
        },
        { label: "Contract number", value: text(card.contract?.number) },
        {
          label: "Contract file",
          value: contractFile ? "Open" : null,
          href: contractFile,
          linkLabel: "Open contract",
        },
      ],
    },
    {
      id: "tax",
      title: "Tax and social security",
      summary: "Your tax number and your BPJS insurance numbers.",
      icon: ShieldCheck,
      intro:
        "The numbers HR uses to withhold your income tax and to keep you covered by BPJS health and employment insurance. Only you and HR can see this.",
      facts: [
        { label: "NPWP (tax number)", value: text(card.npwp) },
        {
          label: "PTKP status",
          value: text(card.ptkp_status),
          note: "PTKP is the tax-free allowance the state gives you; it depends on whether you are single or married and on your dependants, and it sets how much income tax is withheld from your pay.",
        },
        {
          label: "BPJS Kesehatan number",
          value: text(card.bpjs_kesehatan_number),
        },
        {
          label: "BPJS Ketenagakerjaan number",
          value: text(card.bpjs_ketenagakerjaan_number),
        },
      ],
    },
    {
      id: "bank",
      title: "Where my salary goes",
      summary: "The bank account your salary is paid into.",
      icon: Banknote,
      intro:
        "Your salary is transferred to this account. If anything here is wrong, tell HR before the next payday. Only you and HR can see this.",
      facts: [
        { label: "Bank", value: text(card.bank_name) },
        { label: "Account number", value: text(card.bank_account) },
        { label: "Account holder", value: text(card.bank_account_holder) },
      ],
    },
    {
      id: "docs",
      title: "My documents",
      summary: "The papers HR holds for you, ready to open.",
      icon: FolderOpen,
      intro:
        "Copies of your papers kept by HR. Each opens in Google Drive in a new tab. Only you and HR can see this.",
      facts: slotFacts,
    },
  ];
}

function counted(facts: Fact[]): { have: number; total: number } {
  const c = facts.filter((f) => !f.informational);
  return { have: c.filter((f) => f.value != null).length, total: c.length };
}

// ─── Views ────────────────────────────────────────────────────────────

function Header({ card }: { card: HrStaffCard }) {
  const name = text(card.legal_name) ?? text(card.full_name) ?? "My card";
  const where = [text(card.role), text(card.unit) ?? text(card.department)]
    .filter(Boolean)
    .join(" · ");
  const since = formatMonthYear(card.hire_date);
  const end = formatDay(card.end_date);
  const employment = [
    text(card.employer_entity),
    since ? `With us since ${since}` : null,
    end ? `Until ${end}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <header className="mb-8">
      <div className={DESK_RULE} />
      <div className={LABEL}>My card</div>
      <h1
        className={`mt-1 text-[30px] leading-tight sm:text-[34px] ${INK}`}
        style={DESK_SERIF}
      >
        {name}
      </h1>
      {where && <p className={`mt-1 text-[14px] ${INK}`}>{where}</p>}
      {employment && (
        <p className={`mt-1 text-[13px] ${QUIET}`}>{employment}</p>
      )}
    </header>
  );
}

function FactRow({ fact }: { fact: Fact }) {
  const empty =
    fact.emptyText ?? (fact.href === undefined ? NOT_ON_FILE_HR : NOT_ON_FILE);
  return (
    <div
      className={`grid grid-cols-1 gap-x-8 gap-y-1 border-b ${HAIR} py-4 md:grid-cols-[240px_1fr]`}
    >
      <div>
        <div className={LABEL}>{fact.label}</div>
      </div>
      <div className="text-[15px]">
        {fact.value == null ? (
          <span className={QUIET}>{empty}</span>
        ) : fact.href ? (
          <a
            href={fact.href}
            target="_blank"
            rel="noreferrer"
            className={`inline-flex min-h-[44px] items-center underline decoration-[var(--bz-border)] underline-offset-2 ${INK}`}
          >
            {fact.linkLabel ?? fact.value}
          </a>
        ) : (
          <span
            className={`${INK} ${fact.multiline ? "whitespace-pre-line" : ""} break-words`}
          >
            {fact.value}
          </span>
        )}
        {fact.note && (
          <p className={`mt-1 max-w-[62ch] text-[13px] ${QUIET}`}>
            {fact.note}
          </p>
        )}
      </div>
    </div>
  );
}

function Detail({ tile, onBack }: { tile: TileDef; onBack: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  const { have, total } = counted(tile.facts);
  return (
    <section aria-labelledby="card-detail-title">
      <button
        type="button"
        onClick={onBack}
        className={`${LABEL} inline-flex min-h-[44px] items-center underline underline-offset-4`}
      >
        ← Back to my card
      </button>
      <div className={`${DESK_RULE} mt-4`} />
      <h2
        id="card-detail-title"
        ref={heading}
        tabIndex={-1}
        className={`mt-2 text-[26px] focus:outline-none ${INK}`}
        style={DESK_SERIF}
      >
        {tile.title}
      </h2>
      <p className={`mt-1 max-w-[62ch] text-[14px] ${QUIET}`}>{tile.intro}</p>
      <p className={`mt-2 text-[12px] ${QUIET}`}>
        {have} of {total} on file
      </p>
      <div className={`mt-6 border-t ${HAIR}`}>
        {tile.facts.map((f) => (
          <FactRow key={f.label} fact={f} />
        ))}
      </div>
    </section>
  );
}

function Tiles({
  tiles,
  onOpen,
  refs,
}: {
  tiles: TileDef[];
  onOpen: (id: TileId) => void;
  refs: React.MutableRefObject<
    Partial<Record<TileId, HTMLButtonElement | null>>
  >;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {tiles.map((t) => {
        const Icon = t.icon;
        const { have, total } = counted(t.facts);
        return (
          <button
            key={t.id}
            type="button"
            ref={(el) => {
              refs.current[t.id] = el;
            }}
            onClick={() => onOpen(t.id)}
            className={`group flex min-h-[112px] flex-col justify-between border ${HAIR} bg-[var(--bz-card)] p-5 text-left transition-colors hover:border-[var(--bz-copper)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--bz-copper)]`}
          >
            <span className="flex items-center gap-3">
              <Icon className={`h-5 w-5 ${QUIET}`} strokeWidth={1.5} />
              <span className={`text-[18px] ${INK}`} style={DESK_SERIF}>
                {t.title}
              </span>
              <span className="flex-1" />
              <span aria-hidden="true" className={QUIET}>
                →
              </span>
            </span>
            <span className={`mt-3 text-[13px] ${QUIET}`}>{t.summary}</span>
            <span className={`mt-2 text-[12px] ${QUIET}`}>
              {have} of {total} on file
            </span>
          </button>
        );
      })}
    </div>
  );
}

function Skeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading my card">
      <div className="animate-pulse space-y-3">
        <div className="h-4 w-24 rounded bg-[var(--bz-glass-rim)]" />
        <div className="h-8 w-64 rounded bg-[var(--bz-glass-rim)]" />
        <div className="h-4 w-48 rounded bg-[var(--bz-glass-rim)]" />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {[...Array(5)].map((_, i) => (
          <div
            key={i}
            className={`h-[112px] animate-pulse border ${HAIR} bg-[var(--bz-card)]`}
          />
        ))}
      </div>
    </div>
  );
}

type State =
  | { status: "loading" }
  | { status: "loaded"; card: HrStaffCard }
  | { status: "absent" }
  | { status: "failed" };

export default function MyCardPage() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [open, setOpen] = useState<TileId | null>(null);
  const tileRefs = useRef<Partial<Record<TileId, HTMLButtonElement | null>>>(
    {},
  );
  const returnTo = useRef<TileId | null>(null);

  useEffect(() => {
    let mounted = true;
    getMyCard()
      .then((card) => {
        if (mounted) setState({ status: "loaded", card });
      })
      .catch((e: unknown) => {
        if (!mounted) return;
        const code = (e as { statusCode?: number } | null)?.statusCode;
        setState({ status: code === 404 ? "absent" : "failed" });
      });
    return () => {
      mounted = false;
    };
  }, [attempt]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setAttempt((n) => n + 1);
  }, []);

  // Back from a detail view: focus returns to the tile that opened it.
  useEffect(() => {
    if (open === null && returnTo.current) {
      tileRefs.current[returnTo.current]?.focus();
      returnTo.current = null;
    }
  }, [open]);

  if (state.status === "loading") return <Skeleton />;

  if (state.status === "absent") {
    return (
      <div className={`border ${HAIR} bg-[var(--bz-card)] p-6`}>
        <h1 className={`text-[22px] ${INK}`} style={DESK_SERIF}>
          My card
        </h1>
        <p className={`mt-2 text-[14px] ${QUIET}`}>
          Your HR card has not been set up yet. HR will create it.
        </p>
      </div>
    );
  }

  if (state.status === "failed") {
    return (
      <div
        role="alert"
        className="rounded-xl border border-[var(--state-danger)]/30 bg-[var(--state-danger)]/10 p-6 text-[var(--state-danger)]"
      >
        <p>We could not load your card. Please try again.</p>
        <button type="button" onClick={retry} className={`${DESK_PILL} mt-4`}>
          Try again
        </button>
      </div>
    );
  }

  const tiles = buildTiles(state.card);
  const current = tiles.find((t) => t.id === open) ?? null;

  if (current) {
    return (
      <Detail
        tile={current}
        onBack={() => {
          returnTo.current = current.id;
          setOpen(null);
        }}
      />
    );
  }

  return (
    <div>
      <Header card={state.card} />
      <Tiles tiles={tiles} onOpen={setOpen} refs={tileRefs} />
    </div>
  );
}
