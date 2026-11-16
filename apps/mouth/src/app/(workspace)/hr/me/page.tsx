"use client";
/**
 * "My HR": one friendly page where a staff member sees ONLY their own HR data,
 * in three panels (Pay, Time, Me) with the open panel's detail underneath.
 *
 * Personal data: bound to the bearer token's identity per
 * src/lib/auth/SESSION.md S4. The page fetches with requestWithCredential, holds
 * useSessionIdentity(), and keeps a response only if acceptResponse() says it was
 * fetched with THIS identity's bearer and names THIS identity as its owner.
 * Nothing here is logged, cached or persisted.
 */
import React, { useEffect, useMemo, useState } from "react";
import { CalendarHeart, Sparkles } from "lucide-react";
import { DESK_SERIF } from "@balizero/core";
import { ApiError } from "@/lib/api/error-handler";
import { getMyHrOverview, type MyHrOverview } from "@/lib/api/hr/hr";
import {
  acceptResponse,
  isSameIdentity,
  useSessionIdentity,
  type SessionIdentity,
} from "@/lib/auth/session-identity";
import {
  CARD,
  Chip,
  dayLabel,
  daysUntil,
  greeting,
  HAIR,
  INK,
  leaveOf,
  MeDetail,
  PanelCards,
  PayDetail,
  SOFT,
  tenure,
  tint,
  TimeDetail,
  TONE,
  type Panel,
} from "../_components/hr-panels";

type Me = MyHrOverview;

type Load =
  | { kind: "loading" }
  | { kind: "ready"; me: Me }
  | { kind: "no-record" }
  | { kind: "changed" }
  | { kind: "error" };

function Neutral({ children }: { children: React.ReactNode }) {
  return (
    <p
      className={`max-w-[1180px] rounded-2xl border border-dashed ${HAIR} px-4 py-6 text-center text-[14px] ${SOFT}`}
      role="status"
    >
      {children}
    </p>
  );
}

export default function MyHrPage() {
  const identity = useSessionIdentity();
  // Every result is stored with the identity it was fetched for, and shown only
  // while that is still the identity of this render: on a switch, the render
  // that sees the new identity shows nothing, before any effect runs.
  const [slot, setSlot] = useState<{
    owner: SessionIdentity | null;
    load: Load;
  }>({ owner: null, load: { kind: "loading" } });
  const [picked, setPicked] = useState<Panel>("pay");
  const load: Load =
    identity && isSameIdentity(slot.owner, identity)
      ? slot.load
      : { kind: "loading" };
  const me = load.kind === "ready" ? load.me : null;

  useEffect(() => {
    setSlot({ owner: null, load: { kind: "loading" } });
    if (!identity) return;
    let live = true;
    getMyHrOverview()
      .then((res) => {
        if (!live) return;
        setSlot({
          owner: identity,
          load: acceptResponse(res, identity, res.data.team_member_id)
            ? { kind: "ready", me: res.data }
            : { kind: "changed" },
        });
      })
      .catch((err: unknown) => {
        if (!live) return;
        setSlot({
          owner: identity,
          load:
            err instanceof ApiError && err.statusCode === 404
              ? { kind: "no-record" }
              : { kind: "error" },
        });
      });
    return () => {
      live = false;
    };
  }, [identity]);

  const leave = useMemo(() => (me ? leaveOf(me) : null), [me]);
  if (load.kind === "no-record")
    return (
      <Neutral>
        No HR record is linked to your account yet. Ask HR to create it.
      </Neutral>
    );
  if (load.kind === "changed")
    return (
      <Neutral>
        Your session changed, so this page was cleared. Reload to see your own
        HR page.
      </Neutral>
    );
  if (load.kind === "error")
    return (
      <Neutral>We could not load your HR page. Reload to try again.</Neutral>
    );
  if (!me) return <div className="h-64" />;
  const c = me.card;
  // A person whose card says they are not on payroll has no personal payroll to show: no Pay
  // panel, no payslip or bonus block, and no empty "first payslip" nag.
  const paid = c.on_payroll !== false;
  const open: Panel = paid || picked !== "pay" ? picked : "time";
  const ends = daysUntil(c.end_date);
  const first = (c.full_name ?? c.legal_name ?? "").split(/\s+/)[0] || "there";
  const since = tenure(c.hire_date);
  const tone = TONE[open];

  return (
    <div className="sentry-block max-w-[1180px]" data-sentry-block="">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className={`text-[14px] font-medium ${SOFT}`}>{greeting()},</p>
          <h1 className={`text-[40px] leading-tight ${INK}`} style={DESK_SERIF}>
            {first} 👋
          </h1>
          <p className={`mt-1 text-[14px] ${SOFT}`}>
            {[c.position ?? c.role, c.unit, c.employer_entity].filter(Boolean).join(" · ")}
          </p>
        </div>
        {since && (
          <Chip tone={TONE.me}>
            <Sparkles className="h-3.5 w-3.5" aria-hidden /> {since} with Bali
            Zero
          </Chip>
        )}
      </header>

      {ends !== null && ends >= 0 && ends <= 120 && (
        <div
          className="mb-6 flex items-center gap-3 rounded-2xl px-4 py-3 text-[14px]"
          style={{ background: tint("#f59e0b", 14) }}
        >
          <CalendarHeart
            className="h-5 w-5 shrink-0"
            style={{ color: "#f59e0b" }}
            aria-hidden
          />
          <span className={INK}>
            Your contract runs until <b>{dayLabel(c.end_date)}</b> — {ends} days
            from today. HR will talk with you about what comes next.
          </span>
        </div>
      )}

      <div
        className={`grid gap-4 ${paid ? "md:grid-cols-3" : "md:grid-cols-2"}`}
      >
        <PanelCards me={me} open={open} onPick={setPicked} hidePay={!paid} />
      </div>

      <div
        className="mt-6 overflow-hidden rounded-[28px] border p-6 md:p-8"
        style={{ borderColor: tint(tone, 30), background: CARD }}
      >
        <div
          className="-mx-6 -mt-6 mb-8 h-1.5 md:-mx-8 md:-mt-8"
          style={{ background: tone }}
        />
        {open === "pay" && <PayDetail me={me} />}
        {open === "time" && <TimeDetail me={me} leave={leave} />}
        {open === "me" && <MeDetail me={me} />}
        <p className={`mt-8 border-t ${HAIR} pt-4 text-[12px] ${SOFT}`}>
          You can see this, and so can the people authorised to manage HR at
          Bali Zero.
        </p>
      </div>
    </div>
  );
}
