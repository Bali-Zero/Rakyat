"use client";

import { use, useEffect, useRef, useState } from "react";
import {
  AppEmailOptIn,
  AppFrame,
  AppResultTimeline,
  AppShareBar,
  AppStampReveal,
  AppWhatsAppCTA,
  useFunnelApp,
  useHaptic,
  type TimelineCheckpoint,
} from "@balizero/core";
import { ChatAccordion } from "@/components/visa/ChatAccordion";

interface ClockResult {
  hash: string;
  visa_type: string;
  entry_date: string;
  expiry_date: string;
  extensions_possible: number;
  extension_days: number;
  checkpoints: { label: string; at: string; title: string; body: string }[];
  result_url: string;
  session_jwt: string | null;
}

export default function VisaClockResultPage({
  params,
}: {
  params: Promise<{ hash: string }>;
}) {
  const { hash } = use(params);
  const tracker = useFunnelApp("visa_clock", { trackView: false });
  const haptic = useHaptic();
  const stampRef = useRef<HTMLDivElement>(null);
  const [data, setData] = useState<ClockResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch(`/api/visa/clock/${hash}`);
        if (!res.ok) {
          setErr("We could not find this timeline. It may have expired.");
          return;
        }
        const json = (await res.json()) as ClockResult;
        setData(json);
        tracker.resultViewed(json.hash);
        haptic(12); // stamp reveal haptic
      } catch {
        setErr("Network error. Try again.");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash]);

  if (err)
    return (
      <AppFrame funnel="visa" title="Visa Clock" subtitle={err}>
        <p>
          <a href="/visa/clock">Start a new timeline →</a>
        </p>
      </AppFrame>
    );
  if (!data)
    return (
      <AppFrame
        funnel="visa"
        title="Visa Clock"
        subtitle="Loading your timeline…"
      >
        <p style={{ color: "var(--color-text-muted)" }}>One moment.</p>
      </AppFrame>
    );

  const today = witaCivilDay(new Date());
  const checkpoints: TimelineCheckpoint[] = data.checkpoints.map((c) => ({
    ...c,
    past: c.at < today,
  }));
  // SIGNED, deliberately un-clamped, computed as whole civil days in
  // Asia/Makassar (WITA) rather than `Math.ceil((expiry - now) / 86_400_000)`
  // — which yielded `-0` between 00:00 and 08:00 WITA on Day 1 of an overstay
  // and shifted with the viewer's local timezone. Normalized with
  // `diff === 0 ? 0 : diff` so `-0` is impossible.
  const daysToExpiry = civilDaysBetween(today, data.expiry_date) ?? 0;
  const isOverstay = daysToExpiry < 0;
  const daysOverstayed = isOverstay ? -daysToExpiry : 0;
  const daysLeft = isOverstay ? 0 : daysToExpiry === 0 ? 0 : daysToExpiry;
  const publicUrl =
    typeof window !== "undefined"
      ? `${window.location.origin}/visa/clock/${data.hash}`
      : `/visa/clock/${data.hash}`;

  // A permitted stay that already ended is not a timeline with zero days left —
  // it is a different situation, and the countdown UI (stamp, five checkpoints,
  // reminder emails for dates in the past) actively misleads. Hand over to a
  // person instead. Deliberately states NO fines, thresholds or penalties: those
  // are domain facts this page must not invent.
  if (isOverstay)
    return (
      <AppFrame
        funnel="visa"
        title={`Your ${data.visa_type} stay has already ended`}
        subtitle={`Permitted stay ended ${formatIsoDate(data.expiry_date)} — ${daysOverstayed} ${
          daysOverstayed === 1 ? "day" : "days"
        } ago.`}
      >
        <div ref={stampRef} />
        <p style={{ lineHeight: 1.6 }}>
          This tool counts down to an expiry date. Yours has already passed, so
          there is nothing left to count — and showing you a timeline would be
          misleading. What happens next is handled case by case and depends on
          details this form does not ask for.
        </p>
        <p style={{ lineHeight: 1.6 }}>
          Please talk to our visa team today, not in a few days. Bring your
          passport and your entry stamp.
        </p>
        {/* `source` names the FUNNEL APP, and the overstay is a branch of the
            Visa Clock, not a seventh app — its human_name and result_url_path
            would be identical. A value the backend PublicLeadSource enum does
            not carry is rejected with 422, the capture silently falls back to
            the bare wa.me link, and the visitor arrives with no prefilled
            message at all: no visa type, no expiry, no overstay count. That is
            the homepage_hero bug (#2495, 10 days of unlogged leads) and it
            would land on precisely the person least able to absorb it. The
            overstay discriminator therefore travels in `context`, which is
            free-form. */}
        <AppWhatsAppCTA
          source="visa_clock"
          headline="Talk to our visa team today"
          description="Send us the message below and we will pick it up — this is the kind of case that gets harder to fix the longer it waits."
          resultHash={data.hash}
          context={{
            visa_type: data.visa_type,
            entry_date: data.entry_date,
            expiry_date: data.expiry_date,
            overstay: true,
            days_overstayed: daysOverstayed,
          }}
          whatsappContext={[
            { label: "Visa", value: data.visa_type },
            { label: "Entry", value: formatIsoDate(data.entry_date) },
            { label: "Stay ended", value: formatIsoDate(data.expiry_date) },
            { label: "Days overstayed", value: String(daysOverstayed) },
          ]}
          defaultLabel="Message the visa team →"
          postScrollLabel="Message the visa team →"
          stampRef={stampRef}
          onCaptured={({ leadIntentId }) =>
            tracker.whatsappHandoff(leadIntentId)
          }
        />
        <AppShareBar
          url={publicUrl}
          title={`Bali Zero — ${data.visa_type}`}
          onShare={(channel) => tracker.shareClicked(channel)}
        />
      </AppFrame>
    );

  return (
    <AppFrame
      funnel="visa"
      title={`Your ${data.visa_type} timeline`}
      subtitle={`Expires ${formatIsoDate(data.expiry_date)} — ${daysLeft} days from today.`}
      footer={
        <>
          Timeline generated {formatIsoDate(today)}. Government fees may change
          —{" "}
          <a
            href="https://balizero.com/services/visa"
            style={{ color: "var(--accent-funnel-text)" }}
          >
            pricing reference
          </a>
          .
        </>
      }
    >
      {/* Stamp reveal */}
      <div
        ref={stampRef}
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: "var(--space-2, 0.5rem)",
          paddingTop: "var(--space-4, 1.5rem)",
        }}
      >
        <div
          style={{
            fontSize: "0.58rem",
            letterSpacing: "0.2em",
            textTransform: "uppercase",
            opacity: 0.5,
            color: "var(--color-text-muted)",
          }}
        >
          Your visa
        </div>
        <AppStampReveal code={data.visa_type} />
        <p
          style={{
            fontFamily: "var(--font-serif, Georgia, serif)",
            margin: "var(--space-2, 0.5rem) 0 0",
            fontSize: "clamp(1rem, 2.4vw, 1.2rem)",
          }}
        >
          Valid until {formatIsoDate(data.expiry_date)}
        </p>
      </div>

      {/* Timeline section */}
      <section style={{ display: "grid", gap: "var(--space-2, 0.6rem)" }}>
        <div
          style={{
            fontSize: "0.62rem",
            letterSpacing: "0.15em",
            textTransform: "uppercase",
            opacity: 0.5,
            color: "var(--color-text-muted)",
          }}
        >
          Five-checkpoint timeline
        </div>
        <AppResultTimeline
          checkpoints={checkpoints}
          expiryDate={data.expiry_date}
        />
      </section>

      {/* Chat accordion — only present on fresh POST result (session_jwt non-null) */}
      {data.session_jwt ? (
        <ChatAccordion
          checkHash={data.hash}
          sessionJwt={data.session_jwt}
          daysRemaining={daysLeft}
        />
      ) : null}

      {/* Email opt-in */}
      <section>
        <AppEmailOptIn
          app="visa_clock"
          resultHash={data.hash}
          promise="Get reminder emails at each of the 5 checkpoints. One-click unsubscribe."
          payload={{
            visa_type: data.visa_type,
            entry_date: data.entry_date,
            expiry_date: data.expiry_date,
          }}
          endpoint="/api/visa/clock/email"
          onSubscribed={() => tracker.emailSubscribed("clock_5_reminders")}
        />
      </section>

      {/* WhatsApp CTA — sticky after scroll past stamp */}
      <AppWhatsAppCTA
        source="visa_clock"
        headline={`Want our team to file the ${data.visa_type} renewal?`}
        description="Your Bali Zero advisor confirms the fee and the timing for your case. Start on WhatsApp."
        resultHash={data.hash}
        context={{
          visa_type: data.visa_type,
          entry_date: data.entry_date,
          expiry_date: data.expiry_date,
        }}
        whatsappContext={[
          { label: "Visa", value: data.visa_type },
          { label: "Entry", value: formatIsoDate(data.entry_date) },
          { label: "Expiry", value: formatIsoDate(data.expiry_date) },
          { label: "Days left", value: String(daysLeft) },
        ]}
        defaultLabel="Start on WhatsApp →"
        postScrollLabel={`Start the ${data.visa_type} renewal →`}
        stampRef={stampRef}
        onCaptured={({ leadIntentId }) => tracker.whatsappHandoff(leadIntentId)}
      />

      {/* Share bar */}
      <AppShareBar
        url={publicUrl}
        title={`Bali Zero — ${data.visa_type} timeline`}
        onShare={(channel) => tracker.shareClicked(channel)}
      />
    </AppFrame>
  );
}

const WITA_DAY_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Makassar",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function witaCivilDay(now: Date): string {
  const part: Record<string, string> = {};
  for (const p of WITA_DAY_FORMATTER.formatToParts(now)) {
    part[p.type] = p.value;
  }
  return `${part.year}-${part.month}-${part.day}`;
}

function civilDaysBetween(fromDay: string, toDay: string): number | null {
  const a = /^(\d{4})-(\d{2})-(\d{2})$/.exec(fromDay);
  const b = /^(\d{4})-(\d{2})-(\d{2})$/.exec(toDay);
  if (!a || !b) return null;
  const at = Date.UTC(Number(a[1]), Number(a[2]) - 1, Number(a[3]));
  const bt = Date.UTC(Number(b[1]), Number(b[2]) - 1, Number(b[3]));
  if (Number.isNaN(at) || Number.isNaN(bt)) return null;
  const diff = Math.round((bt - at) / 86_400_000);
  return diff === 0 ? 0 : diff;
}

const ISO_DATE_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  day: "numeric",
  month: "short",
  year: "numeric",
});

function formatIsoDate(iso: string): string {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  const d = m
    ? new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
    : new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return ISO_DATE_FORMATTER.format(d);
}
