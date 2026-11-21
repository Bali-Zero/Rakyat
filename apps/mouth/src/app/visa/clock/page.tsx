"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  AppFrame,
  AppHeroForm,
  AppTrustStrip,
  useFunnelApp,
} from "@balizero/core";
import { buildWhatsAppLink } from "@/lib/whatsapp-utm";

// Every `code` MUST be a member of the backend `VisaType` enum
// (apps/backend-rag/backend/services/visa_check/catalogue.py) — an unknown
// code is rejected by `ClockRequest` with 422 before any timeline is built.
// The retired "B211A" tourism code was removed for that reason: C1 (Tourism)
// is its current equivalent, and the backend still maps a cached "B211A"
// submission to C1 for old bookmarks.
const VISA_OPTIONS = [
  { code: "B1", label: "B1 Visa on Arrival (VOA)" },
  { code: "C1", label: "C1 Tourism" },
  { code: "C2", label: "C2 Business" },
  { code: "C7", label: "C7 Art & Culture Performance" },
  { code: "C7A", label: "C7A Music Performance" },
  { code: "C7B", label: "C7B Music Crew" },
  { code: "E33G", label: "E33G Remote Worker / Digital Nomad KITAS" },
  { code: "E33", label: "E33 Second Home KITAS" },
  { code: "E28A", label: "E28A Investor KITAS" },
  { code: "E23", label: "E23 Work KITAS" },
  { code: "E33F", label: "E33F Retirement KITAS (1 Year)" },
  { code: "E31", label: "E31 Family KITAS" },
  { code: "E30A", label: "E30A Student KITAS (Primary & Secondary)" },
];

// "Not sure" is not a visa type, so it is a separate button and never an
// <option> of the Visa type select: the select must only ever hold backend
// `VisaType` codes (guarded by swarm-visa-contracts VISA-3C-01), nothing is
// sent to /api/visa/clock, and the trust strip keeps counting visa types only.
// The button reveals the visa team's existing WhatsApp CTA — we do not guess
// dates for a visa the visitor cannot name.
const NOT_SURE_WHATSAPP_MESSAGE =
  "Hi Bali Zero, I am not sure which visa I am on. Can you help me check my stay dates?";

// Mirrors `ClockRequest._entry_date_reasonable` (visa_check.py): an entry date
// more than 6 years (365 * 6 days, supporting 5-year E33/E33E stay permits) in
// the past or more than 31 days in the future is rejected with 422, so the
// picker must not offer it.
const ENTRY_DATE_MAX_PAST_DAYS = 365 * 6;
const ENTRY_DATE_MAX_FUTURE_DAYS = 31;

function isoDateOffset(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}

const fieldStyle: React.CSSProperties = {
  padding: "0.45rem 0.7rem",
  borderRadius: 4,
  border: "1px solid var(--color-border-subtle)",
  background: "var(--surface-raised)",
  color: "var(--text-primary)",
  fontSize: "inherit",
  fontFamily: "inherit",
};

export default function VisaClockPage() {
  const router = useRouter();
  const tracker = useFunnelApp("visa_clock");
  const [visaType, setVisaType] = useState("");
  const [entryDate, setEntryDate] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notSure, setNotSure] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!visaType || !entryDate) {
      setError("Pick a visa type and the entry date.");
      return;
    }
    setPending(true);
    setError(null);
    tracker.formSubmitted(["visa_type", "entry_date"]);
    // Same silent-death fix as visa/match: capture the HTTP status so the
    // failure event can carry it — endpoint + status only, never the
    // visa type / entry date values (Law 2).
    let status: number | null = null;
    try {
      const res = await fetch("/api/visa/clock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          visa_type: visaType,
          entry_date: entryDate,
          in_country_now: true,
        }),
      });
      // Status recorded for ANY completed response — a 2xx whose JSON
      // parse fails must not be misreported as a network failure (null).
      status = res.status;
      if (!res.ok) {
        throw new Error(`${res.status}`);
      }
      const { hash } = (await res.json()) as { hash: string };
      router.push(`/visa/clock/${hash}`);
    } catch {
      tracker.formSubmitFailed("/api/visa/clock", status);
      setError("Could not build your timeline. Please try again.");
      setPending(false);
    }
  };

  return (
    <AppFrame
      funnel="visa"
      title="Your timeline in up to five checkpoints."
      subtitle="Fill in the two blanks."
      trustStrip={
        <AppTrustStrip
          items={[
            // Both figures describe this tool and are derived, not claimed.
            // The earlier "since 2019" filings figure had no source in any
            // system we run (see lib/trust-figures.ts) and was removed.
            {
              value: String(VISA_OPTIONS.length),
              label: "visa types covered",
            },
            { value: "Up to 5", label: "checkpoints (D-60 → D-1)" },
          ]}
        />
      }
    >
      <AppHeroForm
        headline="Two fields. Your expiry timeline."
        submitLabel="Show my timeline"
        onSubmit={submit}
        pending={pending}
        error={error}
        sentenceTemplate="I entered on {entry} with a {visa} visa."
        sentenceFields={{
          entry: (
            <input
              type="date"
              required
              min={isoDateOffset(-ENTRY_DATE_MAX_PAST_DAYS)}
              max={isoDateOffset(ENTRY_DATE_MAX_FUTURE_DAYS)}
              value={entryDate}
              onChange={(e) => {
                setEntryDate(e.target.value);
                tracker.formStarted("entry_date");
              }}
              style={fieldStyle}
              aria-label="Entry date"
            />
          ),
          visa: (
            <select
              required
              value={visaType}
              onChange={(e) => {
                setVisaType(e.target.value);
                tracker.formStarted("visa_type");
              }}
              style={fieldStyle}
              aria-label="Visa type"
            >
              <option value="">pick one…</option>
              {VISA_OPTIONS.map((v) => (
                <option key={v.code} value={v.code}>
                  {v.label}
                </option>
              ))}
            </select>
          ),
        }}
      />
      <button
        type="button"
        aria-expanded={notSure}
        aria-controls="visa-clock-not-sure"
        onClick={() => setNotSure((open) => !open)}
        style={{
          alignSelf: "flex-start",
          padding: 0,
          border: 0,
          background: "none",
          color: "inherit",
          font: "inherit",
          textDecoration: "underline",
          cursor: "pointer",
        }}
      >
        Not sure which visa you are on? Ask us
      </button>
      {notSure ? (
        <p id="visa-clock-not-sure" style={{ margin: 0, lineHeight: 1.6 }}>
          Our visa team can check it from your passport and entry stamp.{" "}
          <a
            href={buildWhatsAppLink("visa", NOT_SURE_WHATSAPP_MESSAGE)}
            target="_blank"
            rel="noopener noreferrer"
          >
            Ask the visa team on WhatsApp →
          </a>
        </p>
      ) : null}
    </AppFrame>
  );
}
