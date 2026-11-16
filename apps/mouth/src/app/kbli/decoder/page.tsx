import type { Metadata } from "next";
import { KBLIDecoderCTA } from "./_components/KBLIDecoderCTA";
import { KBLIBreadcrumbJsonLd } from "@/components/kbli/KBLIStructuredData";

export const metadata: Metadata = {
  title: "KBLI Decoder — Find Your Business Classification Code",
  description:
    "Not sure which KBLI 2025 code fits your business activity? Map your planned operations to verified Indonesian KBLI 2025 classifications and PT PMA eligibility rules.",
  alternates: {
    canonical: "https://balizero.com/kbli/decoder",
  },
  openGraph: {
    title: "KBLI Decoder — Find Your Business Classification Code",
    description:
      "Not sure which KBLI 2025 code fits your business activity? Map your planned operations to verified Indonesian KBLI 2025 classifications and PT PMA eligibility rules.",
    url: "https://balizero.com/kbli/decoder",
    siteName: "Bali Zero",
    type: "website",
  },
};

export default function KBLIDecoderPage() {
  return (
    <main
      style={{
        background: "var(--surface-base)",
        color: "var(--text-primary)",
        minHeight: "100vh",
      }}
    >
      <KBLIBreadcrumbJsonLd
        items={[
          { name: "KBLI Navigator", url: "https://balizero.com/kbli" },
          { name: "KBLI Decoder", url: "https://balizero.com/kbli/decoder" },
        ]}
      />
      <KBLIDecoderCTA />
    </main>
  );
}
