import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Visa | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Visa" };

export default function PortalVisaLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
