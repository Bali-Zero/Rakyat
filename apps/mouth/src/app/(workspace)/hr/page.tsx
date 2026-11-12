"use client";
/**
 * People: the HR console, read side. Everyone on one roster, and for the selected person
 * the same three panels (Pay, Time, Me) the person sees on their own "My HR" page.
 * HR admins see everyone plus the pulse; the scoped viewer sees a few people, read only.
 * The one write is the card edit (STAFF_CARD.md C11), only for `hr_card_write` viewers (the
 * global admins): an inline form that edits, clears or creates a person's HR card. There is
 * no upload or delete control here.
 *
 * Personal data: bound to the bearer token's identity per src/lib/auth/SESSION.md S4.
 * The roster and each person's overview are fetched with requestWithCredential and kept
 * only if acceptResponse() says they were fetched with THIS identity's bearer and name it
 * (`viewer_team_member_id`) as the reader. Every result is stored with the identity (and,
 * for a person, the member id) it was fetched for and shown only while both still match
 * this render, so a late answer for a previous person or identity is never displayed.
 * The card PATCH answer is accepted under the same rule: only for the identity it was sent
 * with, and only if it names the member it was sent for.
 * Nothing here is logged, cached or persisted.
 */
import React, { Suspense, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import type { LucideIcon } from "lucide-react";
import {
  BadgeCheck,
  CalendarHeart,
  Clock,
  Gift,
  Landmark,
  Pencil,
  Search,
  Sparkles,
  Upload,
  UserPlus,
  Users,
} from "lucide-react";
import { DESK_SERIF } from "@balizero/core";
import { ApiError } from "@/lib/api/error-handler";
import {
  editPersonCard,
  getPeople,
  getPersonOverview,
  type CardEditBody,
  type CardEditFieldError,
  type HrStaffCard,
  type PeopleRoster,
  type PersonOverview,
  type PersonRow,
} from "@/lib/api/hr/hr";
import {
  acceptResponse,
  isSameIdentity,
  useSessionIdentity,
  type SessionIdentity,
} from "@/lib/auth/session-identity";
import { useHrCapabilities } from "@/lib/hr/use-hr-capabilities";
import {
  CardEditForm,
  type CardEditMode,
  type CardEditResult,
} from "./_components/card-edit-form";
import {
  Bubble,
  CARD,
  Chip,
  daysUntil,
  greeting,
  HAIR,
  IDR,
  INK,
  leaveOf,
  MeDetail,
  MONTHS,
  PanelCards,
  PayDetail,
  SOFT,
  tenure,
  tint,
  TimeDetail,
  TONE,
  type Panel,
} from "./_components/hr-panels";

const AMBER = "#f59e0b";
const SOFT_TONE = "#64748b";
const CORAL = "#ef6a3a";
const PALETTE = [
  "#14a37f",
  "#3b82f6",
  "#ef6a3a",
  "#8b5cf6",
  "#e11d74",
  "#0ea5b7",
  "#d97706",
  "#6366f1",
];
const hash = (v: string) => {
  let h = 0;
  for (const ch of v) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
};
const nameOf = (p: PersonRow) => p.name?.trim() || "Unnamed";
const colorOf = (p: PersonRow) =>
  PALETTE[hash(p.team_member_id) % PALETTE.length];
const initials = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");

const GROUP_FIRST = ["Setup", "Tax"];
const GROUP_LAST = ["Office", "Former"];
function groupOf(p: PersonRow): string {
  if (!p.active) return "Former";
  const d = (p.department ?? "").trim();
  return d ? d.charAt(0).toUpperCase() + d.slice(1).toLowerCase() : "Office";
}
function groupRank(g: string) {
  const first = GROUP_FIRST.indexOf(g);
  if (first >= 0) return first;
  const last = GROUP_LAST.indexOf(g);
  return last >= 0 ? 1000 + last : 100;
}

const plural = (n: number, one: string, many: string) =>
  `${n} ${n === 1 ? one : many}`;

function Avatar({ person, size = 40 }: { person: PersonRow; size?: number }) {
  const c = colorOf(person);
  return (
    <span
      className="inline-flex shrink-0 items-center justify-center rounded-full font-semibold text-white"
      style={{
        width: size,
        height: size,
        fontSize: size * 0.38,
        background: `linear-gradient(135deg, ${c}, color-mix(in srgb, ${c} 70%, #000))`,
        boxShadow: `0 8px 18px -10px ${c}`,
      }}
      aria-hidden
    >
      {initials(nameOf(person))}
    </span>
  );
}

function Badge({
  tone,
  label,
  children,
}: {
  tone: string;
  label: string;
  children?: React.ReactNode;
}) {
  // The label is a person's HR state ("2 bonuses waiting"), so it is TEXT, never an
  // attribute: Sentry's click breadcrumbs serialise aria-label and title, not text.
  return (
    <span className="group relative inline-flex">
      {children ? (
        <span
          aria-hidden
          className="rounded-full px-1.5 py-0.5 text-[10px] font-bold"
          style={{ color: tone, background: tint(tone, 16) }}
        >
          {children}
        </span>
      ) : (
        <span
          aria-hidden
          className="inline-block h-2 w-2 rounded-full"
          style={{ background: tone }}
        />
      )}
      <span className="sr-only">{label}</span>
      <span
        aria-hidden
        className="pointer-events-none absolute right-0 bottom-full z-10 mb-1 hidden rounded-md bg-black/80 px-2 py-1 text-[11px] font-medium whitespace-nowrap text-white group-hover:block"
      >
        {label}
      </span>
    </span>
  );
}

function RowBadges({ person }: { person: PersonRow }) {
  const f = person.flags;
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      {f.on_leave_today && <Badge tone={TONE.time} label="On leave today" />}
      {f.bonuses_pending > 0 && (
        <Badge
          tone={AMBER}
          label={plural(f.bonuses_pending, "bonus waiting", "bonuses waiting")}
        />
      )}
      {f.leave_pending > 0 && (
        <Badge
          tone={TONE.time}
          label={plural(
            f.leave_pending,
            "leave request to approve",
            "leave requests to approve",
          )}
        />
      )}
      {!person.has_record && (
        <Badge tone="#94a3b8" label="No HR record yet">
          new
        </Badge>
      )}
      {f.papers_missing != null && f.papers_missing > 0 && (
        <Badge
          tone={CORAL}
          label={plural(f.papers_missing, "paper missing", "papers missing")}
        >
          {f.papers_missing}
        </Badge>
      )}
    </span>
  );
}

function PillLink({
  href,
  tone,
  filledStyle,
  icon: Icon,
  children,
}: {
  href: string;
  tone: string;
  filledStyle?: boolean;
  icon: LucideIcon;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className="inline-flex items-center gap-1.5 rounded-full px-4 py-2 text-[13px] font-semibold transition-all duration-200 hover:-translate-y-0.5"
      style={
        filledStyle
          ? {
              background: tone,
              color: "#fff",
              boxShadow: `0 8px 18px -10px ${tone}`,
            }
          : {
              color: tone,
              border: `1.5px solid ${tint(tone, 55)}`,
              background: tint(tone, 6),
            }
      }
    >
      <Icon className="h-4 w-4" aria-hidden />
      {children}
    </Link>
  );
}

function PulseCard({
  icon,
  tone,
  label,
  value,
  hint,
  href,
}: {
  icon: LucideIcon;
  tone: string;
  label: string;
  value: React.ReactNode;
  hint: React.ReactNode;
  href?: string;
}) {
  const cls =
    "flex min-w-0 flex-col gap-3 rounded-[28px] border p-4 sm:p-5 transition-all duration-200 hover:-translate-y-0.5 hover:shadow-lg";
  const style = { background: tint(tone, 7), borderColor: tint(tone, 26) };
  const inner = (
    <>
      <div className="flex items-center gap-3">
        <Bubble icon={icon} tone={tone} size={36} />
        <span className={`text-[13px] font-semibold ${SOFT}`}>{label}</span>
      </div>
      <div>
        <div
          className={`text-[30px] font-semibold leading-none tracking-tight ${INK}`}
        >
          {value}
        </div>
        <div className={`mt-1.5 text-[13px] ${SOFT}`}>{hint}</div>
      </div>
    </>
  );
  return href ? (
    <Link href={href} className={cls} style={style}>
      {inner}
    </Link>
  ) : (
    <div className={cls} style={style}>
      {inner}
    </div>
  );
}

function Pulse({ roster }: { roster: PeopleRoster }) {
  const p = roster.pulse;
  if (!p) return null;
  const month = MONTHS[p.payroll.month - 1] ?? "";
  const status = p.payroll.status;
  return (
    <div className="mb-8 grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
      <PulseCard
        icon={Users}
        tone={TONE.pay}
        label="Active people"
        value={p.active_people}
        hint={
          p.people_without_record
            ? `${p.people_without_record} without an HR record yet`
            : "everyone has an HR record"
        }
      />
      <PulseCard
        icon={Gift}
        tone={AMBER}
        label="Bonuses waiting"
        value={p.bonuses_pending_count}
        hint={
          p.bonuses_pending_count
            ? `${IDR.format(p.bonuses_pending_idr)} to approve`
            : "nothing waiting"
        }
        href="/hr/bonuses"
      />
      <PulseCard
        icon={CalendarHeart}
        tone={TONE.time}
        label="Leave to approve"
        value={p.leave_pending_count}
        hint={
          p.leave_pending_count
            ? "requests are waiting for a yes"
            : "nothing waiting"
        }
        href="/hr/leave"
      />
      <PulseCard
        icon={Landmark}
        tone={TONE.me}
        label={`Payroll ${month}`}
        value={
          <span className="inline-flex items-center gap-2 text-[19px] leading-tight">
            {status ? (
              <BadgeCheck className="h-5 w-5" aria-hidden />
            ) : (
              <Clock className="h-5 w-5" aria-hidden />
            )}
            <span className="capitalize">{status ?? "Not uploaded yet"}</span>
          </span>
        }
        hint={status ? "this month's payroll period" : "no payroll period yet"}
        href="/hr/payroll"
      />
    </div>
  );
}

type RosterLoad =
  | { kind: "loading" }
  | { kind: "ready"; roster: PeopleRoster }
  | { kind: "changed" }
  | { kind: "error"; forbidden: boolean };
type PersonLoad =
  | { kind: "loading" }
  | { kind: "ready"; data: PersonOverview }
  | { kind: "no-record" }
  | { kind: "changed" }
  | { kind: "error"; forbidden: boolean };

const LOADING = { kind: "loading" } as const;

function Neutral({ children }: { children: React.ReactNode }) {
  return (
    <p
      className={`max-w-[1320px] rounded-2xl border border-dashed ${HAIR} px-4 py-6 text-center text-[14px] ${SOFT}`}
      role="status"
    >
      {children}
    </p>
  );
}

function errorKind(err: unknown) {
  return {
    kind: "error" as const,
    forbidden: err instanceof ApiError && err.statusCode === 403,
  };
}

const PAPER_SLOTS = ["ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk"];
/** The backend's `papers_missing` for a refreshed card (five document slots + the contract file). */
function papersMissing(card: HrStaffCard): number {
  const docs = card.docs ?? {};
  const missing = PAPER_SLOTS.filter((slot) => !docs[slot]).length;
  return missing + (card.contract?.file_id ? 0 : 1);
}

/** A failed card save as the form shows it: fixed texts only, never a value. */
function saveFailure(err: unknown): CardEditResult {
  if (err instanceof ApiError) {
    const detail = (err.data as { detail?: unknown } | undefined)?.detail;
    if (err.statusCode === 422 && Array.isArray(detail)) {
      return {
        ok: false,
        errors: (detail as CardEditFieldError[]).filter(
          (e) => typeof e?.field === "string" && typeof e?.message === "string",
        ),
      };
    }
    if (err.statusCode === 403)
      return { ok: false, errors: [], general: "You cannot edit this card." };
    if (err.statusCode === 409)
      return {
        ok: false,
        errors: [],
        general: "This card is being saved by someone else. Try again.",
      };
    if (err.statusCode === 404)
      return { ok: false, errors: [], general: "This person was not found." };
  }
  return { ok: false, errors: [], general: "Could not save. Try again." };
}

function PeopleConsole() {
  const router = useRouter();
  const params = useSearchParams();
  const { capabilities, loaded, failed } = useHrCapabilities();
  const identity = useSessionIdentity();
  const scope = capabilities.people_scope;
  // A member's HR page is /hr/me; People is for the people scope.
  const toMyHr =
    loaded && !failed && !scope && capabilities.has_employee_record;

  const [rslot, setRslot] = useState<{
    owner: SessionIdentity | null;
    load: RosterLoad;
  }>({ owner: null, load: LOADING });
  const rload: RosterLoad =
    identity && isSameIdentity(rslot.owner, identity) ? rslot.load : LOADING;
  const roster = rload.kind === "ready" ? rload.roster : null;

  const urlPerson = params?.get("person") ?? null;
  const [picked, setPicked] = useState<string | null>(urlPerson);
  useEffect(() => {
    setPicked(urlPerson);
  }, [urlPerson]);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Panel>("pay");
  // The card form is bound to one person and one mode; another selection closes it.
  const [editing, setEditing] = useState<{
    id: string;
    mode: CardEditMode;
  } | null>(null);

  // The selection is only ever a member of the accepted roster.
  const selected: PersonRow | null = useMemo(() => {
    if (!roster) return null;
    return (
      roster.people.find((p) => p.team_member_id === picked) ??
      roster.people[0] ??
      null
    );
  }, [roster, picked]);
  const selectedId = selected?.team_member_id ?? null;
  // The save callback outlives the render that created it: it asks THIS ref who is selected
  // when the answer lands, so a late answer for A never overwrites B's person slot.
  const selectedIdRef = useRef<string | null>(null);
  selectedIdRef.current = selectedId;
  const selectedHasRecord = selected?.has_record ?? false;

  const [pslot, setPslot] = useState<{
    owner: SessionIdentity | null;
    id: string | null;
    load: PersonLoad;
  }>({ owner: null, id: null, load: LOADING });
  const pload: PersonLoad =
    identity &&
    selectedId &&
    isSameIdentity(pslot.owner, identity) &&
    pslot.id === selectedId
      ? pslot.load
      : LOADING;

  useEffect(() => {
    if (toMyHr) router.replace("/hr/me");
  }, [toMyHr, router]);

  useEffect(() => {
    setRslot({ owner: null, load: LOADING });
    if (!identity || !loaded || failed || !scope) return;
    let live = true;
    getPeople()
      .then((res) => {
        if (!live) return;
        setRslot({
          owner: identity,
          load: acceptResponse(res, identity, res.data.viewer_team_member_id)
            ? { kind: "ready", roster: res.data }
            : { kind: "changed" },
        });
      })
      .catch((err: unknown) => {
        if (live) setRslot({ owner: identity, load: errorKind(err) });
      });
    return () => {
      live = false;
    };
  }, [identity, loaded, failed, scope]);

  useEffect(() => {
    setPslot({ owner: null, id: null, load: LOADING });
    if (!identity || !selectedId || !selectedHasRecord) return;
    let live = true;
    getPersonOverview(selectedId)
      .then((res) => {
        if (!live) return;
        const accepted = acceptResponse(
          res,
          identity,
          res.data.viewer_team_member_id,
        );
        setPslot({
          owner: identity,
          id: selectedId,
          load: !accepted
            ? { kind: "changed" }
            : res.data.person.team_member_id === selectedId
              ? { kind: "ready", data: res.data }
              : { kind: "error", forbidden: false },
        });
      })
      .catch((err: unknown) => {
        if (!live) return;
        setPslot({
          owner: identity,
          id: selectedId,
          load:
            err instanceof ApiError && err.statusCode === 404
              ? { kind: "no-record" }
              : errorKind(err),
        });
      });
    return () => {
      live = false;
    };
  }, [identity, selectedId, selectedHasRecord]);

  async function saveCard(body: CardEditBody): Promise<CardEditResult> {
    const id = selectedId;
    const sentFor = identity;
    if (!id || !sentFor)
      return {
        ok: false,
        errors: [],
        general: "Your session changed. Reload to continue.",
      };
    try {
      const res = await editPersonCard(id, body);
      if (
        !acceptResponse(res, sentFor, res.data.viewer_team_member_id) ||
        res.data.person.team_member_id !== id
      )
        return {
          ok: false,
          errors: [],
          general:
            "Your session changed, so the answer was discarded. Reload to check the card.",
        };
      const fresh = res.data.person.card;
      if (selectedIdRef.current === id)
        setPslot({
          owner: sentFor,
          id,
          load: { kind: "ready", data: res.data },
        });
      setRslot((prev) => {
        if (prev.load.kind !== "ready" || !isSameIdentity(prev.owner, sentFor))
          return prev;
        const r = prev.load.roster;
        const old = r.people.find((p) => p.team_member_id === id);
        return {
          owner: prev.owner,
          load: {
            kind: "ready",
            roster: {
              ...r,
              people: r.people.map((p) =>
                p.team_member_id === id
                  ? {
                      ...p,
                      has_record: true,
                      employee_id: fresh.employee_id,
                      hire_date: fresh.hire_date,
                      unit: fresh.unit,
                      active: fresh.is_active,
                      flags: {
                        ...p.flags,
                        papers_missing: papersMissing(fresh),
                      },
                    }
                  : p,
              ),
              pulse:
                r.pulse && old
                  ? {
                      ...r.pulse,
                      people_without_record: Math.max(
                        0,
                        r.pulse.people_without_record -
                          (old.has_record ? 0 : 1),
                      ),
                      active_people: Math.max(
                        0,
                        r.pulse.active_people +
                          (fresh.is_active ? 1 : 0) -
                          (old.active ? 1 : 0),
                      ),
                    }
                  : r.pulse,
            },
          },
        };
      });
      setEditing((cur) => (cur && cur.id === id ? null : cur));
      return { ok: true };
    } catch (err: unknown) {
      return saveFailure(err);
    }
  }

  const grouped = useMemo(() => {
    if (!roster) return [];
    const f = q.trim().toLowerCase();
    const list = roster.people.filter(
      (p) =>
        !f ||
        `${nameOf(p)} ${p.role ?? ""} ${p.unit ?? ""}`
          .toLowerCase()
          .includes(f),
    );
    const groups = new Map<string, PersonRow[]>();
    for (const p of list) {
      const g = groupOf(p);
      groups.set(g, [...(groups.get(g) ?? []), p]);
    }
    return [...groups.entries()].sort(
      ([a], [b]) => groupRank(a) - groupRank(b) || a.localeCompare(b),
    );
  }, [roster, q]);

  const choose = (id: string) => {
    setPicked(id);
    router.replace(`/hr?person=${encodeURIComponent(id)}`, { scroll: false });
  };

  if (failed)
    return (
      <Neutral>
        Could not load your HR access. Reload the page to retry.
      </Neutral>
    );
  if (toMyHr) return null;
  if (loaded && !scope)
    return (
      <Neutral>
        No HR record is linked to your account yet. Ask HR to create it.
      </Neutral>
    );
  if (rload.kind === "changed")
    return (
      <Neutral>
        Your session changed, so this page was cleared. Reload to see People.
      </Neutral>
    );
  if (rload.kind === "error")
    return (
      <Neutral>
        {rload.forbidden
          ? "You do not have access to People."
          : "We could not load People. Reload to try again."}
      </Neutral>
    );
  if (!roster) return <div className="h-64" />;

  const full = roster.scope === "all";
  const canWriteCard = full && capabilities.hr_card_write === true;
  const formMode = editing && editing.id === selectedId ? editing.mode : null;
  const viewer = roster.people.find(
    (p) => p.team_member_id === roster.viewer_team_member_id,
  );
  const first = viewer?.name?.trim().split(/\s+/)[0];
  const person = pload.kind === "ready" ? pload.data.person : null;
  const card = person?.card;
  const since = card ? tenure(card.hire_date, "their first month") : null;
  const ends = card ? daysUntil(card.end_date) : null;
  const tone = TONE[open];

  return (
    <div className="sentry-block max-w-[1320px]" data-sentry-block="">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className={`text-[14px] font-medium ${SOFT}`}>
            {greeting()}
            {first ? `, ${first}` : ""}
          </p>
          <h1 className={`text-[40px] leading-tight ${INK}`} style={DESK_SERIF}>
            People
          </h1>
          <p className={`mt-1 text-[14px] ${SOFT}`}>
            {full
              ? "Everyone at Bali Zero, in one place"
              : "The people you look after"}
          </p>
        </div>
        {full && (
          <div className="flex flex-wrap gap-2">
            <PillLink
              href="/hr/payroll"
              tone={TONE.pay}
              filledStyle
              icon={Upload}
            >
              Upload payroll
            </PillLink>
            <PillLink href="/hr/employees" tone={TONE.pay} icon={UserPlus}>
              Add employee
            </PillLink>
          </div>
        )}
      </header>

      {full && <Pulse roster={roster} />}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[320px_minmax(0,1fr)]">
        <div className="lg:hidden">
          <div className="-mx-4 flex gap-3 overflow-x-auto px-4 pb-2">
            {roster.people.map((p) => (
              <button
                key={p.team_member_id}
                type="button"
                onClick={() => choose(p.team_member_id)}
                aria-pressed={p.team_member_id === selectedId}
                className="flex w-[68px] shrink-0 flex-col items-center gap-1.5"
              >
                {/* The name is the button's text, never an attribute: Sentry's click
                    breadcrumbs record aria-label and title, not text content. */}
                <span className="sr-only">{nameOf(p)}</span>
                <span
                  className="rounded-full p-0.5 transition-all"
                  style={{
                    boxShadow:
                      p.team_member_id === selectedId
                        ? `0 0 0 2.5px ${colorOf(p)}`
                        : "none",
                  }}
                >
                  <Avatar person={p} size={52} />
                </span>
                <span
                  aria-hidden
                  className={`w-full truncate text-center text-[11px] font-medium ${p.team_member_id === selectedId ? INK : SOFT}`}
                >
                  {nameOf(p).split(/\s+/)[0]}
                </span>
              </button>
            ))}
          </div>
        </div>
        <aside className="hidden lg:block">
          <div
            className={`sticky top-4 flex max-h-[calc(100vh-2rem)] flex-col gap-4 overflow-y-auto rounded-[28px] border ${HAIR} p-4`}
            style={{ background: CARD }}
          >
            <label
              className={`flex items-center gap-2 rounded-full border ${HAIR} px-4 py-2.5 focus-within:shadow-md`}
            >
              <Search className={`h-4 w-4 ${SOFT}`} aria-hidden />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search people"
                aria-label="Search people"
                className={`w-full bg-transparent text-[14px] outline-none placeholder:opacity-60 ${INK}`}
              />
            </label>
            {grouped.map(([g, list]) => (
              <div key={g}>
                <p
                  className={`mb-1.5 px-2 text-[11px] font-bold uppercase tracking-wider ${SOFT}`}
                >
                  {g} <span className="opacity-60">· {list.length}</span>
                </p>
                <ul className="flex flex-col gap-0.5">
                  {list.map((p) => {
                    const on = p.team_member_id === selectedId;
                    const col = colorOf(p);
                    return (
                      <li key={p.team_member_id}>
                        <button
                          type="button"
                          onClick={() => choose(p.team_member_id)}
                          aria-pressed={on}
                          className="relative flex w-full items-center gap-3 rounded-2xl py-2 pl-4 pr-3 text-left transition-colors hover:bg-black/[0.04] dark:hover:bg-white/[0.06]"
                          style={on ? { background: tint(col, 14) } : undefined}
                        >
                          {on && (
                            <span
                              className="absolute bottom-2 left-1 top-2 w-1 rounded-full"
                              style={{ background: col }}
                            />
                          )}
                          <Avatar person={p} size={36} />
                          <span className="min-w-0 flex-1">
                            <span
                              className={`block truncate text-[14px] font-semibold ${INK}`}
                            >
                              {nameOf(p)}
                            </span>
                            <span
                              className={`block truncate text-[12px] ${SOFT}`}
                            >
                              {[p.role, p.unit].filter(Boolean).join(" · ")}
                            </span>
                          </span>
                          <RowBadges person={p} />
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
            {grouped.length === 0 && (
              <p className={`px-2 text-[13px] ${SOFT}`}>
                {roster.people.length
                  ? "Nobody matches that search."
                  : "Nobody to show yet."}
              </p>
            )}
          </div>
        </aside>

        <div className="@container min-w-0">
          {!selected ? (
            <div className="h-64" />
          ) : (
            <>
              <div
                className={`mb-6 flex flex-wrap items-center justify-between gap-4 rounded-[28px] border ${HAIR} p-5`}
                style={{
                  background: `linear-gradient(120deg, ${tint(colorOf(selected), 12)}, ${CARD} 70%)`,
                }}
              >
                <div className="flex min-w-0 items-center gap-4">
                  <Avatar person={selected} size={72} />
                  <div className="min-w-0">
                    <h2
                      className={`truncate text-[28px] leading-tight ${INK}`}
                      style={DESK_SERIF}
                    >
                      {nameOf(selected)}
                    </h2>
                    <p className={`text-[14px] ${SOFT}`}>
                      {[
                        card?.role ?? selected.role,
                        card?.unit ?? selected.unit,
                        card?.employer_entity,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {since && (
                        <Chip tone={TONE.me}>
                          <Sparkles className="h-3.5 w-3.5" aria-hidden />{" "}
                          {since}
                        </Chip>
                      )}
                      {ends !== null && ends >= 0 && ends <= 120 && (
                        <Chip tone={AMBER}>
                          <CalendarHeart className="h-3.5 w-3.5" aria-hidden />{" "}
                          contract ends in {ends} days
                        </Chip>
                      )}
                      {card?.on_payroll === false && (
                        <Chip tone={SOFT_TONE}>Not on payroll</Chip>
                      )}
                    </div>
                  </div>
                </div>
                {canWriteCard &&
                  person &&
                  formMode === null &&
                  selected.has_record && (
                    <button
                      type="button"
                      onClick={() =>
                        setEditing({
                          id: selected.team_member_id,
                          mode: "edit",
                        })
                      }
                      className="inline-flex items-center gap-1.5 rounded-full px-4 py-2 text-[13px] font-semibold transition-all duration-200 hover:-translate-y-0.5"
                      style={{
                        color: TONE.me,
                        border: `1.5px solid ${tint(TONE.me, 55)}`,
                        background: tint(TONE.me, 6),
                      }}
                    >
                      <Pencil className="h-4 w-4" aria-hidden />
                      Edit
                    </button>
                  )}
              </div>

              {formMode === "create" ? (
                <CardEditForm
                  key={`${selected.team_member_id}:create`}
                  mode="create"
                  card={null}
                  onSubmit={saveCard}
                  onCancel={() => setEditing(null)}
                />
              ) : formMode === "edit" && person ? (
                <CardEditForm
                  key={`${selected.team_member_id}:edit`}
                  mode="edit"
                  card={person.card}
                  onSubmit={saveCard}
                  onCancel={() => setEditing(null)}
                />
              ) : !selected.has_record || pload.kind === "no-record" ? (
                <div
                  className={`rounded-[28px] border border-dashed ${HAIR} p-8 text-center`}
                >
                  <p className={`text-[16px] font-semibold ${INK}`}>
                    No HR record yet
                  </p>
                  <p className={`mt-1 text-[14px] ${SOFT}`}>
                    There is nothing to show for this person until an HR record
                    exists.
                  </p>
                  {canWriteCard && (
                    <p className="mt-4">
                      <button
                        type="button"
                        onClick={() =>
                          setEditing({
                            id: selected.team_member_id,
                            mode: "create",
                          })
                        }
                        className="inline-flex items-center gap-1.5 rounded-full px-4 py-2 text-[13px] font-semibold text-white transition-all duration-200 hover:-translate-y-0.5"
                        style={{
                          background: TONE.me,
                          boxShadow: `0 8px 18px -10px ${TONE.me}`,
                        }}
                      >
                        <UserPlus className="h-4 w-4" aria-hidden />
                        Add to HR
                      </button>
                    </p>
                  )}
                </div>
              ) : pload.kind === "changed" ? (
                <Neutral>
                  Your session changed, so this person was cleared. Reload to
                  see People.
                </Neutral>
              ) : pload.kind === "error" ? (
                <Neutral>
                  {pload.forbidden
                    ? "You cannot view this person."
                    : "We could not load this person. Select them again to retry."}
                </Neutral>
              ) : !person ? (
                <div className="h-64" />
              ) : (
                <>
                  <div className="grid grid-cols-1 gap-4 @lg:grid-cols-3">
                    <PanelCards
                      me={person}
                      open={open}
                      onPick={setOpen}
                      viewer="admin"
                      compact
                    />
                  </div>
                  <div
                    className="mt-6 overflow-hidden rounded-[28px] border p-6 md:p-8"
                    style={{ borderColor: tint(tone, 30), background: CARD }}
                  >
                    <div
                      className="-mx-6 -mt-6 mb-8 h-1.5 md:-mx-8 md:-mt-8"
                      style={{ background: tone }}
                    />
                    {open === "pay" && (
                      <PayDetail
                        me={person}
                        viewer="admin"
                        payslipLinks={full}
                      />
                    )}
                    {open === "time" && (
                      <TimeDetail
                        me={person}
                        leave={leaveOf(person)}
                        viewer="admin"
                      />
                    )}
                    {open === "me" && <MeDetail me={person} viewer="admin" />}
                    <p
                      className={`mt-8 border-t ${HAIR} pt-4 text-[12px] ${SOFT}`}
                    >
                      Visible only to HR admins.
                    </p>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default function PeoplePage() {
  return (
    <Suspense fallback={<div className="h-64" />}>
      <PeopleConsole />
    </Suspense>
  );
}
