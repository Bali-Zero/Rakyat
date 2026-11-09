import type { Metadata } from "next";
import { R19Presentation } from "@/components/r19/R19Presentation";

// Never indexed, never in the sitemap, never linked, and deliberately NOT in
// robots.txt (a Disallow line would advertise the path).
export const metadata: Metadata = {
  title: "Tes tertulis",
  description: "Halaman tes tertulis.",
  robots: { index: false, follow: false, nocache: true },
};

export default function TaxInterviewLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <R19Presentation force>{children}</R19Presentation>;
}
