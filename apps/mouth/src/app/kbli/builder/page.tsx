import type { Metadata } from "next";
import { KBLIBuilderCTA } from "./_components/KBLIBuilderCTA";
import { KBLIBreadcrumbJsonLd } from "@/components/kbli/KBLIStructuredData";

export const metadata: Metadata = {
  title: "KBLI Builder — Structure KBLI Codes for Your PT PMA",
  description:
    "Setting up a PT PMA in Bali or across Indonesia? Structure compatible primary and secondary KBLI 2025 codes aligned with foreign investment and OSS licensing rules.",
  alternates: {
    canonical: "https://balizero.com/kbli/builder",
  },
  openGraph: {
    title: "KBLI Builder — Structure KBLI Codes for Your PT PMA",
    description:
      "Setting up a PT PMA in Bali or across Indonesia? Structure compatible primary and secondary KBLI 2025 codes aligned with foreign investment and OSS licensing rules.",
    url: "https://balizero.com/kbli/builder",
    siteName: "Bali Zero",
    type: "website",
  },
};

export default function KBLIBuilderPage() {
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
          { name: "KBLI Builder", url: "https://balizero.com/kbli/builder" },
        ]}
      />
      <KBLIBuilderCTA />
    </main>
  );
}
