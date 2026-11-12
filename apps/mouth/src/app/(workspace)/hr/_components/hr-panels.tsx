"use client";
/**
 * Visual primitives and the three panels (Pay, Time, Me) shared by "My HR" (/hr/me) and the
 * People console (/hr). Pure presentation of an overview the caller already accepted: nothing
 * here fetches, stores or logs, and nothing here edits. `viewer` only changes the wording
 * ("Your payslips" for the member, "Payslips" for an HR viewer) and hides the member's own
 * "Request leave" shortcut.
 */
import React from "react";
import Link from "next/link";
import type { LucideIcon } from "lucide-react";
import {
  ArrowUpRight,
  CalendarHeart,
  Check,
  Clock,
  FileText,
  Landmark,
  PartyPopper,
  Plus,
  Sparkles,
  TrendingUp,
  UserRound,
  Wallet,
} from "lucide-react";
import type {
  HrOverviewAttendance,
  HrOverviewLeaveBalance,
  HrOverviewLeaveRequest,
  HrOverviewPayslip,
  HrStaffCard,
  MyHrOverview,
} from "@/lib/api/hr/hr";

export const INK = "text-[var(--tx-pure,var(--bz-text-1))]";
export const SOFT = "text-[var(--tx-secondary,var(--bz-text-2))]";
export const HAIR = "border-[var(--bz-border)]";
export const CARD = "var(--bz-card, var(--bz-surface, transparent))";

export const TONE = { pay: "#14a37f", time: "#3b82f6", me: "#ef6a3a" } as const;
export type Panel = keyof typeof TONE;
export const tint = (c: string, pct: number) =>
  `color-mix(in srgb, ${c} ${pct}%, ${CARD})`;
export const STATUS_TONE: Record<string, string> = {
  approved: "#14a37f",
  paid: "#14a37f",
  pending: "#f59e0b",
  draft: "#f59e0b",
  rejected: "#ef4444",
  cancelled: "#94a3b8",
  reversed: "#ef4444",
};

export type Card = HrStaffCard;
export type Slip = HrOverviewPayslip;
export type Att = HrOverviewAttendance;
export type Me = MyHrOverview;

export const IDR = new Intl.NumberFormat("id-ID", {
  style: "currency",
  currency: "IDR",
  maximumFractionDigits: 0,
});
export const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
export const SHORT = MONTHS.map((m) => m.slice(0, 3));

export function ymd(v: string | null | undefined) {
  const m = v ? /^(\d{4})-(\d{2})(?:-(\d{2}))?/.exec(v) : null;
  return m ? { y: +m[1], mo: +m[2], d: m[3] ? +m[3] : 1 } : null;
}
export const monthLabel = (v: string) => {
  const p = ymd(v);
  return p ? `${MONTHS[p.mo - 1]} ${p.y}` : v;
};
export const monthOnly = (v: string) => {
  const p = ymd(v);
  return p ? MONTHS[p.mo - 1] : v;
};
export const dayLabel = (v: string | null | undefined) => {
  const p = ymd(v);
  return p ? `${p.d} ${SHORT[p.mo - 1]} ${p.y}` : null;
};
export const shortDay = (v: string | null | undefined) => {
  const p = ymd(v);
  return p ? `${p.d} ${SHORT[p.mo - 1]}` : "";
};
// Bali Zero runs on Asia/Makassar (UTC+8): "today" and "this month" are cut there,
// the same zone the server uses for bonus months and attendance.
export const WITA_DAY = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Makassar",
});
export const witaDay = (d: Date) => WITA_DAY.format(d);
export const todayIso = () => witaDay(new Date());
export const slipMonth = (p: Slip) =>
  `${p.payroll_year}-${String(p.payroll_month).padStart(2, "0")}`;
export function daysUntil(v: string | null | undefined) {
  const p = ymd(v);
  if (!p) return null;
  return Math.round(
    (Date.UTC(p.y, p.mo - 1, p.d) - Date.parse(todayIso() + "T00:00:00Z")) /
      86400000,
  );
}
export function tenure(
  v: string | null | undefined,
  firstMonth = "your first month",
) {
  const p = ymd(v);
  if (!p) return null;
  const t = new Date();
  let months = (t.getUTCFullYear() - p.y) * 12 + (t.getUTCMonth() + 1 - p.mo);
  if (t.getUTCDate() < p.d) months -= 1;
  if (months < 1) return firstMonth;
  const y = Math.floor(months / 12),
    m = months % 12;
  return [
    y && `${y} year${y > 1 ? "s" : ""}`,
    m && `${m} month${m > 1 ? "s" : ""}`,
  ]
    .filter(Boolean)
    .join(" and ");
}
export const greeting = () => {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
};
export const UNVERIFIED = "On file — not yet in DOKUMEN KARYAWAN";
export const driveUrl = (id: string | null | undefined) =>
  id && /^[A-Za-z0-9_-]{10,200}$/.test(id)
    ? `https://drive.google.com/file/d/${id}/view`
    : null;
export const s = (c: Card, k: keyof Card) => {
  const v = c[k];
  return typeof v === "string" && v.trim() ? v : null;
};

export function Pill({ status }: { status: string }) {
  const c = STATUS_TONE[status.toLowerCase()] ?? "#94a3b8";
  return (
    <span
      className="inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-semibold capitalize"
      style={{ color: c, background: tint(c, 14) }}
    >
      {status}
    </span>
  );
}

export function Chip({
  tone,
  children,
}: {
  tone: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px] font-semibold"
      style={{ color: tone, background: tint(tone, 14) }}
    >
      {children}
    </span>
  );
}

export function Bubble({
  icon: Icon,
  tone,
  size = 40,
}: {
  icon: LucideIcon;
  tone: string;
  size?: number;
}) {
  return (
    <span
      className="inline-flex shrink-0 items-center justify-center rounded-2xl text-white"
      style={{
        width: size,
        height: size,
        background: tone,
        boxShadow: `0 6px 16px -6px ${tone}`,
      }}
    >
      <Icon style={{ width: size * 0.5, height: size * 0.5 }} aria-hidden />
    </span>
  );
}

export function PayBars({ slips, tone }: { slips: Slip[]; tone: string }) {
  const list = [...slips].reverse();
  const vals = list.map((p) => p.net_salary_idr);
  const max = Math.max(...vals),
    min = Math.min(...vals);
  return (
    <div className="flex h-[64px] items-end gap-2" aria-hidden>
      {list.map((p, i) => (
        <div key={p.id} className="flex flex-1 flex-col items-center gap-1">
          <span
            className="w-full max-w-[22px] rounded-t-full rounded-b-md"
            style={{
              height: `${16 + (max > min ? ((p.net_salary_idr - min) / (max - min)) * 30 : 30)}px`,
              background: i === list.length - 1 ? tone : tint(tone, 28),
            }}
          />
          <span className={`text-[10px] ${SOFT}`}>
            {monthOnly(slipMonth(p)).slice(0, 1)}
          </span>
        </div>
      ))}
    </div>
  );
}

export function Ring({
  left,
  used,
  pending,
  tone,
}: {
  left: number;
  used: number;
  pending: number;
  tone: string;
}) {
  const total = Math.max(1, left + used + pending);
  const r = 30,
    c = 2 * Math.PI * r;
  const seg = (v: number) => (Math.max(0, v) / total) * c;
  const parts = [
    { v: seg(left), color: tone },
    { v: seg(pending), color: "#f59e0b" },
    { v: seg(used), color: tint(tone, 22) },
  ];
  let offset = 0;
  return (
    <svg
      width="76"
      height="76"
      viewBox="0 0 76 76"
      aria-hidden
      className="shrink-0"
    >
      <circle
        cx="38"
        cy="38"
        r={r}
        fill="none"
        stroke={tint(tone, 12)}
        strokeWidth="9"
      />
      {parts.map((p, i) => {
        const el = (
          <circle
            key={i}
            cx="38"
            cy="38"
            r={r}
            fill="none"
            stroke={p.color}
            strokeWidth="9"
            strokeDasharray={`${p.v} ${c - p.v}`}
            strokeDashoffset={-offset}
            strokeLinecap="butt"
            transform="rotate(-90 38 38)"
          />
        );
        offset += p.v;
        return el;
      })}
    </svg>
  );
}

export function Progress({
  have,
  total,
  tone,
}: {
  have: number;
  total: number;
  tone: string;
}) {
  return (
    <div
      className="h-3 w-full overflow-hidden rounded-full"
      style={{ background: tint(tone, 14) }}
      aria-hidden
    >
      <div
        className="h-full rounded-full"
        style={{
          width: `${(have / Math.max(1, total)) * 100}%`,
          background: tone,
        }}
      />
    </div>
  );
}

export function PanelCard({
  id,
  open,
  onPick,
  icon,
  title,
  children,
}: {
  id: Panel;
  open: Panel;
  onPick: (p: Panel) => void;
  icon: LucideIcon;
  title: string;
  children: React.ReactNode;
}) {
  const tone = TONE[id];
  const active = open === id;
  return (
    <button
      type="button"
      onClick={() => onPick(id)}
      aria-pressed={active}
      className="group flex flex-col gap-4 rounded-[28px] border p-5 text-left transition-all duration-200 hover:-translate-y-0.5"
      style={{
        background: tint(tone, active ? 13 : 7),
        borderColor: active ? tone : tint(tone, 26),
        boxShadow: active ? `0 14px 30px -18px ${tone}` : "none",
      }}
    >
      <span className="flex items-center justify-between">
        <span className="flex items-center gap-3">
          <Bubble icon={icon} tone={tone} />
          <span className={`text-[15px] font-semibold ${INK}`}>{title}</span>
        </span>
        <span
          className="rounded-full px-2.5 py-1 text-[11px] font-semibold transition-colors"
          style={{
            color: active ? "#fff" : tone,
            background: active ? tone : tint(tone, 16),
          }}
        >
          {active ? "Open" : "See more"}
        </span>
      </span>
      {children}
    </button>
  );
}

export function Section({
  title,
  icon,
  tone,
  action,
  children,
}: {
  title: string;
  icon: LucideIcon;
  tone: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="min-w-0">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <span className="flex items-center gap-2.5">
          <Bubble icon={icon} tone={tone} size={30} />
          <h3 className={`text-[17px] font-semibold ${INK}`}>{title}</h3>
        </span>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Facts({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="grid gap-2 sm:grid-cols-2">
      {rows.map(([k, v]) => (
        <div key={k} className={`rounded-2xl border ${HAIR} px-4 py-3`}>
          <dt className={`text-[11px] font-semibold ${SOFT}`}>{k}</dt>
          <dd
            className={`mt-0.5 text-[14px] break-words [overflow-wrap:anywhere] whitespace-pre-line ${v ? INK : SOFT}`}
          >
            {v || "Not on file yet"}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function List({ children }: { children: React.ReactNode }) {
  return <ul className="flex flex-col gap-2">{children}</ul>;
}
export function Row({
  left,
  sub,
  right,
  href,
}: {
  left: React.ReactNode;
  sub?: React.ReactNode;
  right: React.ReactNode;
  href?: string;
}) {
  const inner = (
    <>
      <span className="min-w-0">
        <span className={`block text-[14px] font-medium break-words ${INK}`}>
          {left}
        </span>
        {sub ? <span className={`text-[12px] ${SOFT}`}>{sub}</span> : null}
      </span>
      <span className="flex shrink-0 flex-col items-end gap-1">{right}</span>
    </>
  );
  const cls = `flex items-center justify-between gap-4 rounded-2xl border ${HAIR} px-4 py-3`;
  return (
    <li>
      {href ? (
        <Link
          href={href}
          className={`${cls} transition-transform hover:-translate-y-0.5`}
        >
          {inner}
        </Link>
      ) : (
        <div className={cls}>{inner}</div>
      )}
    </li>
  );
}

export type Viewer = "self" | "admin";

export function PayDetail({
  me,
  viewer = "self",
  payslipLinks = true,
}: {
  me: Me;
  viewer?: Viewer;
  /** The payslip page is for HR admins and the owner of the slip: a scoped viewer gets no link. */
  payslipLinks?: boolean;
}) {
  const you = viewer === "self";
  const c = me.card;
  const tone = TONE.pay;
  const thisMonth = todayIso().slice(0, 7);
  const ytd = me.bonus_totals.year_total_idr;
  return (
    <div
      className={`grid grid-cols-1 gap-10 ${you ? "lg:grid-cols-2" : "@3xl:grid-cols-2"}`}
    >
      <div className="flex flex-col gap-10">
        <Section
          title={you ? "Your payslips" : "Payslips"}
          icon={Wallet}
          tone={tone}
        >
          {me.payslips.length === 0 ? (
            <p
              className={`rounded-2xl border border-dashed ${HAIR} px-4 py-6 text-center text-[14px] ${SOFT}`}
            >
              {you
                ? "Your first payslip will show up here after payday."
                : "No payslip on file yet."}
            </p>
          ) : (
            <List>
              {me.payslips.map((p, i) => (
                <Row
                  key={p.id}
                  href={payslipLinks ? `/hr/payroll/${p.id}` : undefined}
                  left={
                    <span>
                      {monthLabel(slipMonth(p))}{" "}
                      {i === 0 && (
                        <span className="ml-1">
                          <Chip tone={tone}>Latest</Chip>
                        </span>
                      )}
                    </span>
                  }
                  sub={
                    <span className="hidden sm:inline">
                      {`Base ${IDR.format(p.base_salary_idr)} · bonuses ${IDR.format(p.bonus_total_idr)} · deductions ${IDR.format(p.deduction_total_idr)}`}
                    </span>
                  }
                  right={
                    <span className={`text-[15px] font-semibold ${INK}`}>
                      {IDR.format(p.net_salary_idr)}
                    </span>
                  }
                />
              ))}
            </List>
          )}
        </Section>
        <Section
          title={you ? "Where your salary lands" : "Where the salary lands"}
          icon={Landmark}
          tone={tone}
        >
          <Facts
            rows={[
              ["Bank", s(c, "bank_name")],
              ["Account", s(c, "bank_account")],
              ["Account holder", s(c, "bank_account_holder")],
              ["Tax number (NPWP)", s(c, "npwp")],
              ["Tax status (PTKP)", s(c, "ptkp_status")],
            ]}
          />
        </Section>
      </div>
      <Section
        title={you ? "Your bonuses this year" : "Bonuses this year"}
        icon={PartyPopper}
        tone={tone}
        action={<Chip tone={tone}>{IDR.format(ytd)}</Chip>}
      >
        {me.bonuses.length === 0 ? (
          <p
            className={`rounded-2xl border border-dashed ${HAIR} px-4 py-6 text-center text-[14px] ${SOFT}`}
          >
            {you
              ? "No bonuses yet this year — they appear here as soon as a practice is awarded."
              : "No bonuses yet this year."}
          </p>
        ) : (
          <List>
            {me.bonuses.slice(0, 12).map((b) => {
              const day = witaDay(new Date(b.awarded_at));
              return (
                <Row
                  key={b.id}
                  left={b.practice_type_code || "Practice"}
                  sub={
                    day.startsWith(thisMonth)
                      ? `This month · ${shortDay(day)}`
                      : dayLabel(day)
                  }
                  right={
                    <>
                      <span className={`text-[14px] font-semibold ${INK}`}>
                        {IDR.format(b.amount_idr)}
                      </span>
                      <Pill status={b.status} />
                    </>
                  }
                />
              );
            })}
            {me.bonuses.length > 12 && (
              <li className={`px-1 text-[12px] ${SOFT}`}>
                and {me.bonuses.length - 12} more earlier this year
              </li>
            )}
          </List>
        )}
      </Section>
    </div>
  );
}

export function TimeDetail({
  me,
  leave,
  viewer = "self",
}: {
  me: Me;
  leave: ReturnType<typeof leaveOf>;
  viewer?: Viewer;
}) {
  const you = viewer === "self";
  const tone = TONE.time;
  const worked = me.attendance.filter((a) => a.days_present > 0);
  return (
    <div
      className={`grid grid-cols-1 gap-10 ${you ? "lg:grid-cols-2" : "@3xl:grid-cols-2"}`}
    >
      <Section
        title={you ? "Your time off" : "Time off"}
        icon={CalendarHeart}
        tone={tone}
        action={
          you ? (
            <Link
              href="/hr/leave/request"
              className="inline-flex items-center gap-1.5 rounded-full px-4 py-2 text-[13px] font-semibold text-white transition-transform hover:-translate-y-0.5"
              style={{
                background: tone,
                boxShadow: `0 8px 18px -10px ${tone}`,
              }}
            >
              <Plus className="h-4 w-4" aria-hidden /> Request leave
            </Link>
          ) : undefined
        }
      >
        {leave && (
          <div className="mb-4 flex flex-wrap gap-2">
            <Chip tone={tone}>{leave.left} days left</Chip>
            <Chip tone="#94a3b8">{leave.used} used</Chip>
            {leave.pending > 0 && (
              <Chip tone="#f59e0b">{leave.pending} waiting for approval</Chip>
            )}
          </div>
        )}
        {me.leave.requests.length === 0 ? (
          <p
            className={`rounded-2xl border border-dashed ${HAIR} px-4 py-6 text-center text-[14px] ${SOFT}`}
          >
            {you
              ? "You have not asked for any leave yet. Time for a break?"
              : "No leave requested yet."}
          </p>
        ) : (
          <List>
            {me.leave.requests.slice(0, 8).map((r) => (
              <Row
                key={r.id}
                left={
                  r.start_date === r.end_date
                    ? dayLabel(r.start_date)
                    : `${shortDay(r.start_date)} → ${dayLabel(r.end_date)}`
                }
                sub={`${r.leave_type_name} · ${r.total_days} day${r.total_days === 1 ? "" : "s"}`}
                right={<Pill status={r.status} />}
              />
            ))}
          </List>
        )}
      </Section>
      <Section
        title={you ? "Your time at work" : "Time at work"}
        icon={Clock}
        tone={tone}
      >
        {worked.length === 0 ? (
          <p
            className={`rounded-2xl border border-dashed ${HAIR} px-4 py-6 text-center text-[14px] ${SOFT}`}
          >
            No clock-in recorded yet.
          </p>
        ) : (
          <List>
            {worked.map((a) => {
              const late = a.late_incidents ?? 0;
              const onTime = Math.max(0, a.days_present - late);
              return (
                <li
                  key={a.month}
                  className={`rounded-2xl border ${HAIR} px-4 py-3`}
                >
                  <div className="flex items-baseline justify-between">
                    <span className={`text-[14px] font-medium ${INK}`}>
                      {monthLabel(a.month)}
                    </span>
                    <span className={`text-[14px] font-semibold ${INK}`}>
                      {a.days_present} days
                      {a.hours != null ? ` · ${Math.round(a.hours)} h` : ""}
                    </span>
                  </div>
                  <div
                    className="mt-2 flex h-2.5 overflow-hidden rounded-full"
                    style={{ background: tint(tone, 12) }}
                    aria-hidden
                  >
                    <span
                      style={{
                        width: `${(onTime / Math.max(1, a.days_present)) * 100}%`,
                        background: TONE.pay,
                      }}
                    />
                    <span
                      style={{
                        width: `${(late / Math.max(1, a.days_present)) * 100}%`,
                        background: "#f59e0b",
                      }}
                    />
                  </div>
                  <p className={`mt-1.5 text-[12px] ${SOFT}`}>
                    Usually in at {a.avg_clock_in ?? "—"}
                    {a.late_incidents === null
                      ? ""
                      : ` · ${onTime} on time${late ? ` · ${late} late` : " · never late"}`}
                  </p>
                </li>
              );
            })}
          </List>
        )}
      </Section>
    </div>
  );
}

export const SLOTS: [string, string][] = [
  ["ktp", "ID card (KTP)"],
  ["kk", "Family card (KK)"],
  ["npwp", "Tax card (NPWP)"],
  ["bpjs_kes", "BPJS Kesehatan"],
  ["bpjs_tk", "BPJS Ketenagakerjaan"],
];

export function MeDetail({
  me,
  viewer = "self",
  showPapers = true,
}: {
  me: Me;
  viewer?: Viewer;
  // The People console renders its own Documents block (STAFF_CARD.md C13) in place of these chips.
  showPapers?: boolean;
}) {
  const you = viewer === "self";
  const c = me.card;
  const tone = TONE.me;
  const none = { present: false, file_id: null };
  const papers: [string, { present: boolean; file_id: string | null }][] = [
    ["Contract", c.documents?.contract ?? none],
    ...SLOTS.map(
      ([k, l]) =>
        [l, c.documents?.[k as keyof typeof c.documents] ?? none] as [
          string,
          { present: boolean; file_id: string | null },
        ],
    ),
  ];
  return (
    <div
      className={`grid grid-cols-1 gap-10 ${you ? "lg:grid-cols-2" : "@3xl:grid-cols-2"}`}
    >
      <Section title={you ? "About you" : "About"} icon={UserRound} tone={tone}>
        <Facts
          rows={[
            ["Legal name", s(c, "legal_name")],
            ["ID number (NIK)", s(c, "nik")],
            [
              "Born",
              [s(c, "birth_place"), dayLabel(s(c, "birth_date"))]
                .filter(Boolean)
                .join(", ") || null,
            ],
            ["Gender", s(c, "gender")],
            ["Marital status", s(c, "marital_status")],
            ["Religion", s(c, "religion")],
            ["Address", s(c, "address")],
          ]}
        />
      </Section>
      <div className="flex flex-col gap-10">
        <Section title={you ? "Your job" : "Job"} icon={Sparkles} tone={tone}>
          <Facts
            rows={[
              ["Employer", s(c, "employer_entity")],
              [
                "Role",
                [s(c, "role"), s(c, "unit")].filter(Boolean).join(" · ") ||
                  null,
              ],
              ["Started", dayLabel(s(c, "hire_date"))],
              ["Contract no.", c.contract?.number ?? null],
              ["Contract ends", dayLabel(s(c, "end_date"))],
              ["BPJS Kesehatan", s(c, "bpjs_kesehatan_number")],
              ["BPJS Ketenagakerjaan", s(c, "bpjs_ketenagakerjaan_number")],
            ]}
          />
        </Section>
        {showPapers && (
          <Section
            title={you ? "Your papers" : "Papers"}
            icon={FileText}
            tone={tone}
          >
            <div className="flex flex-wrap gap-2">
              {papers.map(([label, doc]) => {
                const url = driveUrl(doc.file_id);
                return url ? (
                  <a
                    key={label}
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    referrerPolicy="no-referrer"
                    className="inline-flex items-center gap-1.5 rounded-full px-3.5 py-2 text-[13px] font-semibold transition-transform hover:-translate-y-0.5"
                    style={{ color: tone, background: tint(tone, 14) }}
                  >
                    <Check className="h-3.5 w-3.5" aria-hidden /> {label}{" "}
                    <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
                  </a>
                ) : (
                  <span
                    key={label}
                    className={`inline-flex items-center rounded-full border border-dashed ${HAIR} px-3.5 py-2 text-[13px] ${SOFT}`}
                  >
                    {doc.present
                      ? `${label} · ${UNVERIFIED}`
                      : `${label} · missing`}
                  </span>
                );
              })}
            </div>
            {you && (
              <p className={`mt-3 text-[12px] ${SOFT}`}>
                Something missing or wrong? Tell HR and they will fix it.
              </p>
            )}
          </Section>
        )}
      </div>
    </div>
  );
}

export function leaveOf(me: Me) {
  const a = me.leave.balances.find(
    (b: HrOverviewLeaveBalance) => b.code === "annual",
  );
  if (!a) return null;
  const total = a.allocated_days + a.carried_over;
  return {
    total,
    used: a.used_days,
    pending: a.pending_days,
    left: total - a.used_days - a.pending_days,
  };
}

export function filled(c: Card) {
  const keys: (keyof Card)[] = [
    "legal_name",
    "nik",
    "birth_place",
    "birth_date",
    "gender",
    "marital_status",
    "religion",
    "address",
    "employer_entity",
    "hire_date",
    "npwp",
    "ptkp_status",
    "bpjs_kesehatan_number",
    "bpjs_ketenagakerjaan_number",
    "bank_name",
    "bank_account",
    "bank_account_holder",
  ];
  const papers = Object.values(c.documents ?? {});
  const onFile = papers.filter((d) => d.present).length;
  const have = keys.filter((k) => s(c, k)).length + onFile;
  const missingPapers = 6 - onFile;
  return { have, total: keys.length + 6, missingPapers };
}

/**
 * The three summary cards (Pay, Time, Me) that open the detail underneath. `viewer` picks the
 * wording; `compact` is the narrower People layout (smaller numbers, ring only when wide).
 */
export function PanelCards({
  me,
  open,
  onPick,
  viewer = "self",
  compact = false,
  hidePay = false,
}: {
  me: Me;
  open: Panel;
  onPick: (p: Panel) => void;
  viewer?: Viewer;
  compact?: boolean;
  /** An off-payroll member's own page: no Pay panel at all. */
  hidePay?: boolean;
}) {
  const you = viewer === "self";
  const c = me.card;
  const leave = leaveOf(me);
  const slip = me.payslips[0];
  const prev = me.payslips[1];
  const thisMonth = todayIso().slice(0, 7);
  const monthBon = me.bonus_totals;
  const att = me.attendance.find((a) => a.month === thisMonth) ?? null;
  const done = filled(c);
  const nextLeave = me.leave.requests
    .filter(
      (r: HrOverviewLeaveRequest) =>
        r.start_date >= todayIso() && /approved|pending/i.test(r.status),
    )
    .sort((x, y) => x.start_date.localeCompare(y.start_date))[0];
  const diff = slip && prev ? slip.net_salary_idr - prev.net_salary_idr : 0;
  const big = compact ? "text-[22px] @2xl:text-[28px]" : "text-[32px]";
  return (
    <>
      {!hidePay && (
        <PanelCard
          id="pay"
          open={open}
          onPick={onPick}
          icon={Wallet}
          title="Pay"
        >
          <span>
            <span
              className={`block ${big} font-semibold leading-none tracking-tight ${INK}`}
            >
              {slip ? IDR.format(slip.net_salary_idr) : "—"}
            </span>
            <span className={`mt-1.5 block text-[13px] ${SOFT}`}>
              {slip
                ? `${you ? "Your take-home" : "Take-home"} for ${monthLabel(slipMonth(slip))}`
                : you
                  ? "Your first payslip is on its way"
                  : "No payslip yet"}
            </span>
          </span>
          {me.payslips.length > 1 ? (
            <PayBars slips={me.payslips} tone={TONE.pay} />
          ) : (
            <div className="h-[64px]" />
          )}
          <span className="flex flex-wrap gap-2">
            {diff > 0 && (
              <Chip tone={TONE.pay}>
                <TrendingUp className="h-3.5 w-3.5" aria-hidden /> +
                {IDR.format(diff)} vs {monthOnly(slipMonth(prev!))}
              </Chip>
            )}
            <Chip tone={TONE.pay}>
              <PartyPopper className="h-3.5 w-3.5" aria-hidden />{" "}
              {monthBon.month_count
                ? `${IDR.format(monthBon.month_total_idr)} bonuses this month`
                : "No bonus yet this month"}
            </Chip>
          </span>
        </PanelCard>
      )}

      <PanelCard
        id="time"
        open={open}
        onPick={onPick}
        icon={CalendarHeart}
        title="Time"
      >
        <span className="flex items-center gap-4">
          {leave ? (
            <span className={compact ? "hidden @2xl:block" : undefined}>
              <Ring
                left={leave.left}
                used={leave.used}
                pending={leave.pending}
                tone={TONE.time}
              />
            </span>
          ) : null}
          <span>
            <span
              className={`block ${big} font-semibold leading-none tracking-tight ${INK}`}
            >
              {leave ? `${leave.left} days` : "—"}
            </span>
            <span className={`mt-1.5 block text-[13px] ${SOFT}`}>
              {leave
                ? `of holiday left out of ${leave.total}`
                : "No leave balance yet"}
            </span>
          </span>
        </span>
        <span className="flex flex-wrap gap-2">
          {nextLeave && (
            <Chip tone={TONE.time}>
              Next off: {shortDay(nextLeave.start_date)}
              {/pending/i.test(nextLeave.status) ? " (waiting)" : ""}
            </Chip>
          )}
          <Chip tone={TONE.time}>
            <Clock className="h-3.5 w-3.5" aria-hidden />{" "}
            {att
              ? `${att.days_present} days at work this month`
              : "No clock-in this month yet"}
          </Chip>
        </span>
        {att?.avg_clock_in && (
          <span className={`text-[12px] ${SOFT}`}>
            Usually in at {att.avg_clock_in}
            {att.late_incidents === null
              ? ""
              : att.late_incidents
                ? ` · ${att.late_incidents} late`
                : " · never late"}
          </span>
        )}
      </PanelCard>

      <PanelCard
        id="me"
        open={open}
        onPick={onPick}
        icon={UserRound}
        title="Me"
      >
        <span>
          <span
            className={`block ${big} font-semibold leading-none tracking-tight ${INK}`}
          >
            {done.have}
            <span className={SOFT}>/{done.total}</span>
          </span>
          <span className={`mt-1.5 block text-[13px] ${SOFT}`}>
            details and papers on file
          </span>
        </span>
        <Progress have={done.have} total={done.total} tone={TONE.me} />
        <span className="flex flex-wrap gap-2">
          {done.missingPapers ? (
            <Chip tone={TONE.me}>
              {done.missingPapers} paper{done.missingPapers === 1 ? "" : "s"}{" "}
              {you ? "to add — HR can help" : "missing"}
            </Chip>
          ) : (
            <Chip tone={TONE.pay}>
              <Check className="h-3.5 w-3.5" aria-hidden /> All set
            </Chip>
          )}
        </span>
      </PanelCard>
    </>
  );
}
