"use client";

import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { WhatsAppLeadButton } from "@/components/lead/WhatsAppLeadButton";

const DECODER_WA_CONTEXT: { label: string; value: string }[] = [
  { label: "Service", value: "KBLI Decoder" },
  { label: "Page", value: "/kbli/decoder" },
];

const PAIN_POINTS: string[] = [
  "Choosing the wrong KBLI code can stall PT PMA incorporation or OSS licensing",
  "1,559 KBLI 2025 codes — a single business model often spans multiple classifications",
  "Foreign ownership caps (Perpres 10/2021 & 49/2021) and risk tiers (PP 28/2025) vary code by code",
];

const STEPS: { step: string; text: string }[] = [
  {
    step: "1",
    text: "Describe your planned business activity via WhatsApp",
  },
  {
    step: "2",
    text: "Our corporate team maps your activity to the appropriate KBLI 2025 code(s)",
  },
  {
    step: "3",
    text: "Receive recommended KBLI codes with foreign ownership and licensing notes",
  },
];

export function KBLIDecoderCTA() {
  return (
    <section className="max-w-2xl mx-auto px-4 py-12 md:py-20">
      <span
        className="inline-block text-xs font-semibold uppercase tracking-widest px-3 py-1 rounded-full mb-6"
        style={{
          background: "var(--r19-wash)",
          color: "var(--accent-funnel)",
        }}
      >
        KBLI 2025
      </span>

      <h1
        className="text-3xl md:text-4xl font-bold mb-4 leading-tight"
        style={{ color: "var(--text-primary)" }}
      >
        KBLI Decoder
      </h1>

      <p
        className="text-base md:text-lg mb-8"
        style={{ color: "var(--text-secondary)" }}
      >
        Not sure which KBLI code fits your business? Tell us what you plan to
        operate — our team maps your activity across all 1,559 KBLI 2025
        classifications and checks PT PMA eligibility.
      </p>

      <div className="mb-12">
        <WhatsAppLeadButton
          source="kbli_decoder"
          whatsappContext={DECODER_WA_CONTEXT}
          className="inline-flex items-center gap-2 px-6 py-3 rounded-xl text-sm font-semibold"
          style={{
            background: "var(--accent-funnel)",
            color: "var(--text-on-accent)",
          }}
        >
          Start Consultation on WhatsApp →
        </WhatsAppLeadButton>
      </div>

      <div className="mb-10">
        <h2
          className="text-xs font-semibold uppercase tracking-widest mb-4"
          style={{ color: "var(--text-secondary)" }}
        >
          Why the right code matters
        </h2>
        <ul className="space-y-3" role="list">
          {PAIN_POINTS.map((point) => (
            <li key={point} className="flex items-start gap-3">
              <AlertTriangle
                size={15}
                className="shrink-0 mt-0.5"
                style={{ color: "var(--accent-funnel)" }}
                aria-hidden="true"
              />
              <span
                className="text-sm leading-relaxed"
                style={{ color: "var(--text-secondary)" }}
              >
                {point}
              </span>
            </li>
          ))}
        </ul>
      </div>

      <div className="mb-12">
        <h2
          className="text-xs font-semibold uppercase tracking-widest mb-4"
          style={{ color: "var(--text-secondary)" }}
        >
          How it works
        </h2>
        <ol className="space-y-5" role="list">
          {STEPS.map(({ step, text }) => (
            <li key={step} className="flex items-start gap-4">
              <span
                className="shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold"
                style={{
                  background: "var(--r19-wash)",
                  color: "var(--accent-funnel)",
                }}
                aria-hidden="true"
              >
                {step}
              </span>
              <span
                className="text-sm leading-relaxed pt-0.5"
                style={{ color: "var(--text-secondary)" }}
              >
                {text}
              </span>
            </li>
          ))}
        </ol>
      </div>

      <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
        Prefer to browse codes yourself?{" "}
        <Link
          href="/kbli"
          className="underline underline-offset-2"
          style={{ color: "var(--text-primary)" }}
        >
          Open KBLI Navigator →
        </Link>
      </p>
    </section>
  );
}
