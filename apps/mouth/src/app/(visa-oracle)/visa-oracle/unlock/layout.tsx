import type { Metadata } from "next";

// The unlock page is a client component and cannot export metadata, so its
// head is set here. Own canonical, not the one inherited from ../layout.tsx
// (which names /visa-oracle): the page is an internal team door, kept
// noindex/nofollow exactly as the parent sets it, and nothing public links
// to it.
export const metadata: Metadata = {
  title: "Visa Oracle Unlock",
  alternates: { canonical: "https://balizero.com/visa-oracle/unlock" },
  robots: { index: false, follow: false },
};

export default function VisaOracleUnlockLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
