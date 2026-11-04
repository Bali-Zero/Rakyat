import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Billing | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Billing" };

export default function PortalBillingLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
