"use client";

import { usePathname } from "next/navigation";
import { WhatsAppLeadButton } from "@/components/lead/WhatsAppLeadButton";
import { buildWhatsAppLink } from "@/lib/whatsapp-utm";

/**
 * Pages that carry their own filled red CTA. On these the header button
 * steps down to an outline, so the page shows exactly one red CTA:
 * /kbli/[code] (KBLIConsultationCTA), /kbli/builder, /kbli/decoder.
 */
const OWN_PRIMARY_CTA = [
  /^\/kbli\/\d{5}\/?$/,
  /^\/kbli\/(builder|decoder)\/?$/,
];

export function pageHasOwnPrimaryCTA(pathname: string | null): boolean {
  return !!pathname && OWN_PRIMARY_CTA.some((re) => re.test(pathname));
}

export function KBLIHeaderCTA() {
  const secondary = pageHasOwnPrimaryCTA(usePathname());

  return (
    <WhatsAppLeadButton
      source="kbli_navigator"
      whatsappContext={[{ label: "Source", value: "KBLI Navigator" }]}
      utm={{ page: "/kbli" }}
      fallbackHref={buildWhatsAppLink("kbli")}
      className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-md text-[11px] font-semibold uppercase tracking-wide"
      style={
        secondary
          ? {
              background: "transparent",
              color: "var(--accent-funnel-text)",
              boxShadow: "inset 0 0 0 1px var(--accent-funnel)",
            }
          : {
              background: "var(--accent-funnel)",
              color: "var(--text-on-accent)",
            }
      }
    >
      Get Started
    </WhatsAppLeadButton>
  );
}
